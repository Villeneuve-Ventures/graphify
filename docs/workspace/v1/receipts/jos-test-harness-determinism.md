# JOS test-harness determinism accepted completion receipt

Receipt status: `ACCEPTED`

Surfaces:

- `JOS-BACKEND-DETECTION-TEST-ISOLATION`
- `JOS-GIT-SEED-HISTORY-STABILITY`

Accepted at live refresh: `2026-08-03T02:37:33Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

This receipt accepts historical closure evidence for exactly two repository
test-harness follow-ups that PR #45 preserved for separate authorization:

- backend-detection tests clear every API-key selector declared by the live
  `BACKENDS` registry through `_backend_env_keys()`, ignore malformed non-string
  dynamic entries, and also clear the Azure endpoint, all Bedrock/AWS region or
  profile selectors, and `OLLAMA_BASE_URL` read directly by `detect_backend()`;
  and
- the shared P3 synthetic seed commit fixes only its author, committer,
  timestamps, signing, and hook inputs so equivalent fixtures retain one commit
  identity across hostile inherited Git environments, while the exact
  persistent-source-replacement regression retains its security meaning.

The implementation boundary is the three test/helper files delivered by PR #47.
Production `graphify/llm.py`, backend priority and behavior, source-identity
policy, schemas, dependencies, workflows, configuration, and generated graph
remain unchanged.

`CLOSED` is historical evidence only. It grants no product, phase, successor,
provider, credential, network, fallback, semantic-sync, source-identity,
implementation, execution, mutation, or user-state authority.

## Original defect evidence

- Pull request [#45](https://github.com/Villeneuve-Ventures/graphify/pull/45)
  preserved both defects under its “Justified deferrals” section rather than
  broadening the P5B2 semantic-worker delivery.
- Immutable PR #45 node: `PR_kwDOTZvP8s75bnmy`; its exact live body at this
  closeout refresh retained SHA-256
  `8b3ab5a6a3a28c05fa1c142da5c2c5c5767ec453efb73fc2e7fdc9c69d8fb50f`.
- Exact PR #45 revision: base
  `99af03803a44d575123a18f1c0eafa48149df492`, delivery head
  `5f57e565bd188789c984bc1370943caa758148c3`, merge
  `36b2e3426ebe3095a0b81c36656789b6790f103f`, and delivery/merge tree
  `06d20480337bc94edba4de37c06d2dbf1ab595f2`.
- Backend reproduction: with `GEMINI_API_KEY=governance-evidence`, the original
  focused selection reported `3 failed, 1 passed, 9 deselected, 1 warning`;
  the Ollama, Kimi-over-Ollama, and no-provider cases inherited ambient Gemini.
- Seed-history reproduction: a disposable bare-repository probe held tree,
  author, committer, and message constant while shifting author and committer
  time by one second. The commit IDs differed:
  `6e51d74b1e04ae12a0e8f0d24cd3f96edaa5dac7` versus
  `9730502641837cb4f8ac399b4d772156dc4b61d2`.
- The original records were PR-description deferrals, not review threads;
  thread identity, path/line, resolution, and outdated state are
  `not-applicable`.

## Exact PR #47 delivery evidence

- Pull request [#47](https://github.com/Villeneuve-Ventures/graphify/pull/47),
  merged into `workspace/v1` at `2026-08-03T01:14:08Z`.
- Immutable pull-request node: `PR_kwDOTZvP8s75vPSD`; the exact live PR body at
  closeout refresh had SHA-256
  `c85257f95753429b603ecaca889cf69322ac805fb4a3d8a3fa8520398ac06cc7`.
- Exact delivery base:
  `c2bb53d733d43784b76ab3cf559c48c16688f298`.
- Exact delivery head:
  `e17482c61a5cfad2d227a4b0d8d27c2bcd723c32`.
- Merge/current canonical commit:
  `d19ff5467a48778b14a4cdb62eada4ba3fa48293`.
- Delivery-head, merge, and current canonical tree:
  `8b2fc5a29c06eb7df2a41cd79c896e052636a19e`.
- The merge's first direct parent is the exact delivery base and its second
  direct parent is the exact delivery head. The base is the delivery merge
  base, the delivery head is reachable from the merge, and delivery, merge,
  current canonical, and live `origin/workspace/v1` trees are identical.
- The delivery changed exactly these three files:

```text
tests/test_ollama.py
tests/test_workspace_generations.py
tests/workspace_p3_helpers.py
```

## Exact-head hosted validation

- GitHub Actions run
  [30771565129](https://github.com/Villeneuve-Ventures/graphify/actions/runs/30771565129)
  was associated with exact delivery head
  `e17482c61a5cfad2d227a4b0d8d27c2bcd723c32`, completed, and succeeded.
- `skillgen-check`, `test (3.14)`, and `security-scan` all completed
  successfully; the separate exact-head `CodeRabbit` status context reported
  `Review completed` and succeeded.
- The hosted Python 3.14 full-suite job reported `5,133 passed, 4 skipped, 5
  warnings` in `549.14s`. The isolated installation smoke also passed.

## Review/thread disposition

- GitHub reported seven top-level comments, three submitted reviews, and one
  review thread. The sole thread,
  `PRRT_kwDOTZvP8s6V0fgA`, is current (`isOutdated=false`) and resolved by its
  bot owner, `qodo-code-review[bot]`; zero threads remain unresolved. No
  comment, review, or thread was replied to, resolved, or otherwise mutated
  during this closeout.
- The thread's exact-head comment,
  [discussion 3700333413](https://github.com/Villeneuve-Ventures/graphify/pull/47#discussion_r3700333413),
  identified malformed dynamic provider keys. Final-head code filters
  non-string entries, and the focused dynamic-provider regression passes.
- CodeRabbit's earlier reviews requested proof that the dynamic provider is
  selected before cleanup and isolation from inherited Git hooks. Both are
  present at the exact delivery head and covered by passing regressions.
- The persistent reviewer guide was updated through exact head
  `e17482c61a5cfad2d227a4b0d8d27c2bcd723c32` and reported no major issue or
  security concern. Qodo's later review record shows zero open bugs,
  requirement gaps, or rule violations and marks its single bug resolved.
- Every top-level comment, including the later `/prreview` trigger and the
  exact-head persistent-review update, was inspected through the final comment
  update at `2026-08-03T01:14:19Z`; none records a remaining actionable defect.

## Focused local validation

Fresh validation ran from canonical merge
`d19ff5467a48778b14a4cdb62eada4ba3fa48293`, whose tree is byte-identical to
the exact PR #47 delivery-head tree:

- Hostile backend-selector command with ambient Gemini, Google, Kimi, Claude,
  OpenAI, DeepSeek, Azure, Bedrock/AWS, and Ollama selectors:
  `4 passed, 10 deselected, 1 warning`.
- `uv run --frozen --all-extras pytest -q tests/test_ollama.py`:
  `14 passed, 1 warning`.
- `uv run --frozen --all-extras pytest -q tests/test_workspace_generations.py
  -k 'persistent_source_replacement or deterministic_seed'`:
  `2 passed, 74 deselected, 1 warning`.

The backend result proves provider-neutral fixtures under the complete ambient
selector set. The seed result independently proves stable commit identity
across different inherited author/committer identities and dates, enabled
signing, and a failing inherited hook, plus the exact persistent replacement's
trusted-parent and distinct-head assertions.

## Governance-closeout preflight

- The canonical checkout and live `origin/workspace/v1` were clean and equal at
  `d19ff5467a48778b14a4cdb62eada4ba3fa48293`, tree
  `8b2fc5a29c06eb7df2a41cd79c896e052636a19e`, with local divergence `0/0`.
- One worktree existed, clean on `workspace/v1` at that exact canonical head.
  No delivery worktree, competing governance worktree, open fork pull request,
  or other closeout owner existed. The fork's issue tracker is disabled.
- PR #45 source provenance, PR #47 merged state, parentage, exact three-file
  manifest, tree parity, hosted CI, all comments and reviews, and current
  review-thread state were independently revalidated with repository-qualified
  GitHub operations.
- Observed support baseline: host CPython `3.14.6`, project environment CPython
  `3.14.3`, and uv `0.11.30`.

## Governance-closeout validation

- The advisory documentation-diff audit confirmed documentation-only scope.
  Its material-claim and identity notices were reconciled to live Git/GitHub,
  immutable PR objects, code, tests, hosted run evidence, and current authority.
- Every relative Markdown link and target anchor in the three changed files
  resolved, and the changed-file manifest matched exactly the three authorized
  Markdown paths.
- `uv lock --check`, `uv run --frozen python -m tools.skillgen --check`,
  `uv run --frozen pre-commit run --all-files`, and `git diff --check` passed.
- The independent documentation/governance review is performed against the
  committed exact head. Its disposition is reported with the pull request and
  closeout handoff rather than recursively embedded in the commit it reviews.

## Excluded effects

This governance-only closeout changes no product code, tests, helpers, schemas,
dependencies, workflows, configuration, generated graph, or external plan. It
performs no phase promotion, successor activation, full semantic sync,
backend/provider behavior change, production or user-state mutation, GitHub
comment/review-thread reply or resolution, merge, canonical fast-forward,
branch/worktree cleanup, or unrelated governance work.

It grants no named or headless backend; provider discovery; credential,
network, endpoint, or fallback behavior; source-identity policy; sealed-input
finalization; certification; promotion; pointer mutation; migrate; broader
repair, GC, mutation, or query authority; installation; watch/service;
publication; performance/resource proof; H3; P6+; or cleanup authority.

## Closeout disposition

`JOS-BACKEND-DETECTION-TEST-ISOLATION` and
`JOS-GIT-SEED-HISTORY-STABILITY` transition together from
`SEPARATE_AUTHORIZATION_REQUIRED` to `CLOSED` because each independent closure
contract is satisfied by the same exact PR #47 test-harness boundary and this
separate receipt.

Parent P5 and P5B2 remain `IN_PROGRESS`; H3 remains `DEFERRED`; remaining P5B2
commands, broad P5C, and remaining P5C concerns remain `WAITING`; and no phase
or successor is promoted. This receipt authorizes no merge, canonical
fast-forward, cleanup, graph refresh, external-plan mutation, or later work.
