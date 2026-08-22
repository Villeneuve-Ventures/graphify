# P5B2 semantic-release decision-store and capacity/GC accepted completion receipt

Receipt status: `ACCEPTED`

Surface: `P5B2 semantic-release decision-store and capacity/GC` (unnumbered
prerequisite)

Accepted at live refresh: `2026-08-22T21:31:53Z`

Repository authority state: `STAGED`. This governance-only receipt proposal
becomes repo-local accepted evidence only when its commit is separately
published and merged into `Villeneuve-Ventures/graphify@workspace/v1`. Until
then, the published canonical branch remains authoritative.

## Frozen scope

This receipt accepts only the internal persistence and shared-capacity boundary
frozen in
[`../semantic-sync.md`](../semantic-sync.md#p5b2-semantic-release-decision-store-and-capacitygc-prerequisite).
Acceptance requires PR #76's contract freeze, PR #77's implementation delivery,
and PR #79's canonical-directory correction together. PR #77 alone does not
satisfy the final accepted boundary.

The accepted surface is exactly:

- private `SemanticReleaseDecisionStore` ownership of the request-addressed
  mode-`0600` binding namespace and its bounded non-authoritative publication
  staging;
- the frozen canonical binding shapes and digest preimages, install-once and
  byte-identical replay behavior, and fail-closed commit-uncertainty rules;
- independent 25 MiB-per-binding, 64-bindings-per-generation, and
  4,096-bindings-per-workspace limits plus existing global/workspace byte,
  durable-reservation, and filesystem-reserve accounting;
- existing registry/workspace/generation lock-order integration and exact
  current-directory rebinding after capacity and GC scans; and
- shared workspace reachability treatment that blocks the GC lifecycle while
  any nonempty decision state exists.

Acceptance provisions no live policy-authority or decision record. It creates
no classification composition, omission, projection, public CLI/schema/runtime
receipt, provider/backend, network, publication, release, canonical-state
cleanup, binding deletion, repair, quarantine, rollback, GC mutation, parent
completion, or successor authority.

## Delivery evidence

### PR #76 contract freeze

- Pull request: [#76](https://github.com/Villeneuve-Ventures/graphify/pull/76),
  merged at `2026-08-18T01:07:56Z`.
- Exact base: `33c7d8255b18128f8371219c823f78f6cbb010f6`.
- Exact head: `94ef3ba716f18504705569a584b1fb03b29d4c42`.
- Merge commit: `13a5abe45a14bc7051bc2f82c1bf183ade59ed67`.
- Head, merge, and delivery tree:
  `8fa8b78ba33b598a5ae7e32262114e7508cc038d`.
- Exact-head CI [32086058260](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32086058260)
  and exact-merge CI [32087107935](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32087107935)
  passed `skillgen-check`, `test (3.14)`, and `security-scan`.
- Changed manifest: the seven maintained-current contract documents in
  `docs/workspace/v1/` and no code, tests, schemas, receipts, or generated
  output.
- Review inventory: 16 top-level timeline comments, three submitted
  `COMMENTED` reviews, and six threads: one resolved/outdated and five
  unresolved/outdated.

### PR #77 implementation delivery

- Pull request: [#77](https://github.com/Villeneuve-Ventures/graphify/pull/77),
  merged at `2026-08-19T15:58:01Z`.
- Exact base: `13a5abe45a14bc7051bc2f82c1bf183ade59ed67`.
- Exact head: `28d204cf66fc4026a8cd631d4a6462d64575063e`.
- Merge commit: `e4b930ca0073d2404216f7392f78a192a49ab9b5`.
- Head, merge, and delivery tree:
  `15603dd51f187e86da19c5ed0d66ada44def5dd7`.
- Exact-head CI [32258592787](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32258592787),
  exact-head PR Agent [32271545087](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32271545087),
  and exact-merge CI [32273175871](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32273175871)
  passed. CI passed `skillgen-check`, `test (3.14)`, and `security-scan`; PR
  Agent passed `review`.
- Changed manifest: the seven maintained-current contract documents;
  `graphify/workspace/composition.py`, `contracts.py`, `gc.py`,
  `generations.py`, `persistence.py`, and `semantic_release_decision.py`; and
  `tests/test_workspace_generations.py` plus
  `tests/test_workspace_semantic_release_decision.py`.
- Review inventory: 30 top-level timeline comments, seven submitted
  `COMMENTED` reviews, and 16 threads: nine resolved, six current unresolved,
  and one unresolved/outdated.

### PR #79 canonical-directory correction

- Pull request: [#79](https://github.com/Villeneuve-Ventures/graphify/pull/79),
  merged at `2026-08-22T20:34:08Z`.
- Exact base: `a3e5021fbdfcc3fe8e70fc75b34a0214fc3b03d2`.
- Exact head: `26f4025274d9cd2184397fbbfde22aa8baf2f98d`.
- Merge/current canonical commit:
  `c54c6116a45cf026546579b2fd6421fbad6dcf74`.
- Head, merge, and current canonical tree:
  `5d5de9f6b7bcbc3fae2a0d51399b262a896ffe90`.
- Exact-head CI [32593448372](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32593448372),
  exact-head PR Agent [32596616049](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32596616049),
  and exact-merge CI [32597059464](https://github.com/Villeneuve-Ventures/graphify/actions/runs/32597059464)
  passed. CI passed `skillgen-check`, `test (3.14)`, and `security-scan`; PR
  Agent passed `review`.
- Changed manifest:

```text
graphify/workspace/generations.py
tests/test_workspace_semantic_release_decision.py
```

- Review inventory: three top-level timeline comments, no submitted reviews,
  and zero review threads.

PR #78 and PR #80 are intervening ancestry, not acceptance evidence. PR #78
merged `3cc2dfe154e91bd6a29f416d8d242fb16010bb79`, tree
`eafd90c43827ad822363108dc61a40e4cf7046f7`, and changed repository guidance
and translated-readme state. PR #80 merged
`a3e5021fbdfcc3fe8e70fc75b34a0214fc3b03d2`, tree
`66a601954c64961e6ace08ebe0304b1d2ffc68f9`, and changed only `uv.lock`.
Neither changes the selected semantic outcome or acceptance proof boundary.

## Exact-current-tree review disposition

GitHub review state was inspected read-only and was not treated as technical
proof. No comment, reply, review, thread, or other GitHub review state was
mutated. Every substantive concern is dispositioned against current canonical
tree `5d5de9f6b7bcbc3fae2a0d51399b262a896ffe90`:

| Pull request / thread | Concern | Exact-current-tree disposition |
|---|---|---|
| #76 `PRRT_kwDOTZvP8s6Z5Hnt` | Placeholder UUIDs did not match the frozen path examples. | Fixed in PR #76's final contract and the maintained-current documents. |
| #76 `PRRT_kwDOTZvP8s6Z5kqZ` | Classification could begin before the capture locks were released. | Fixed: capture releases registry/workspace/generation locks before classification and reacquires the frozen final-install order afterward. |
| #76 `PRRT_kwDOTZvP8s6Z7H_Q` | The contract did not state the lock release before classification precisely enough. | Fixed by the maintained capture/classify/reacquire sequence and its focused tests. |
| #76 `PRRT_kwDOTZvP8s6Z7H_S` | Binding-count limits had to remain independent of `CapacityPolicy`. | Fixed: 25 MiB, 64-per-generation, and 4,096-per-workspace are separate non-caller-expandable bounds. |
| #76 `PRRT_kwDOTZvP8s6Z7H_U` | A new public GC protection-reason token would widen the public contract. | Fixed: shared reachability blocks the lifecycle without adding public reason vocabulary. |
| #76 `PRRT_kwDOTZvP8s6Z7H_V` | A pristine absent namespace needed an explicit zero-state rule. | Fixed: safely observed top-level absence is zero binding state; present unsafe or ambiguous state fails closed. |
| #77 `PRRT_kwDOTZvP8s6aGkxs` | Empty crash residue could brick the decision namespace. | Fixed by PR #77 commit `200784e6b4b837ad10ad452449208ffdca21106a` and its previsibility, interrupted-staging, and retry regressions. |
| #77 `PRRT_kwDOTZvP8s6aGkxy` | Optional stable missing reads could bypass the deadline. | Fixed by `test_optional_stable_missing_read_checks_deadline_after_observation`. |
| #77 `PRRT_kwDOTZvP8s6aG7Oi` | A timestamped mutable preflight snapshot was compared with the later PR head. | Rejected: the row explicitly records its independent-review preflight time and uncommitted working-tree digest; it is historical evidence, not a claim about PR #77's final head. |
| #77 `PRRT_kwDOTZvP8s6aG7Or` | The delivered GC blocker scope was understated. | Fixed: maintained authority records that shared workspace reachability blocks preview/plan and therefore execute, reconcile, and purge. |
| #77 `PRRT_kwDOTZvP8s6aG7O-` | The capacity binding read loop did not retry `InterruptedError`. | Fixed by PR #77 commit `200784e6b4b837ad10ad452449208ffdca21106a` and `test_decision_capacity_scan_retries_interrupted_binding_read`. |
| #77 `PRRT_kwDOTZvP8s6aG7PH` | The store could derive a workspace path outside shared `GenerationStore` authority. | Fixed by reusing the registered `GenerationStore` workspace and its existing lock/capacity ownership. |
| #77 `PRRT_kwDOTZvP8s6aG7PQ` | Repeated global capacity scans were unnecessarily expensive. | Deferred: the retained fail-closed scans are bounded and correctness-preserving, while scan optimization and performance/resource qualification remain separate P5C work outside this prerequisite. |
| #77 `PRRT_kwDOTZvP8s6aG7Pa` | A non-raw, overly broad test regex triggered Ruff and weak matching. | Fixed in PR #77's final test suite. |
| #77 `PRRT_kwDOTZvP8s6aOkAx` | Cleanup sorting performed unguarded repeated `stat` calls. | Fixed by PR #77 commit `6799d7d6af35d5ee40f03804e33834b2e1131c70` and cleanup-entry revalidation regressions. |
| #77 `PRRT_kwDOTZvP8s6aOkA2` | A Python 3.14 unparenthesized multi-exception clause was reported as Python 2 syntax. | Rejected: `pyproject.toml` requires Python 3.14, where PEP 758 permits this grammar; the exact module compiles and targeted Pyright and tests pass. |
| #77 `PRRT_kwDOTZvP8s6aOkA3` | Two test harness bindings were unused. | Fixed in PR #77's final test suite. |
| #77 `PRRT_kwDOTZvP8s6aQcWU` | `NO_MATCH` could incorrectly retain an omission disposition. | Fixed by PR #77 commit `d900219a66f0fed9da2cf331ef44c8c478ec3a32` and `test_no_match_cannot_omit_rationale`. |
| #77 `PRRT_kwDOTZvP8s6aVrve` | Double-dot semantic entity IDs could enter a persisted binding. | Fixed by PR #77 commit `00d33254732fd2a595b7cd9faf0aade41213c6af` and `test_binding_rejects_double_dot_semantic_entity_id`. |
| #77 `PRRT_kwDOTZvP8s6aVrvf` | Persisted classifier results did not enforce match/indeterminate invariants. | Fixed by `test_match_requires_private_rule_provenance` and `test_indeterminate_cannot_retain_match_provenance`. |
| #77 `PRRT_kwDOTZvP8s6adwH6` | Pre-parent-fsync publication uncertainty could be misreported as committed. | Fixed by PR #77 head `28d204cf66fc4026a8cd631d4a6462d64575063e` and the three post-publication durability regressions. |
| #77 `PRRT_kwDOTZvP8s6ahsvj` | Capacity and GC scans could validate a detached decision directory after canonical replacement. | Fixed by PR #79 commit `3723a42e1ffb3262764c1c8b5bbe7d3527fbbe2d` and the two exact canonical-directory regressions. |

GitHub's resolved/outdated presentation is immutable historical UI state for
this closeout, not acceptance evidence. PR #76 remains one resolved/outdated
plus five unresolved/outdated threads; PR #77 remains nine resolved, six
current unresolved, and one unresolved/outdated; PR #79 remains thread-free.

## Local acceptance validation

The exact current canonical tree and staged documentation candidate passed these
required local gates:

- exact canonical-directory regressions: `2 passed` in `1.21s`;
- exact six-file decision-store/capacity/GC suite: `652 passed` in `152.55s`;
- targeted Ruff over the delivered implementation and tests: passed;
- targeted repository-configured Pyright over the same surface: zero errors,
  warnings, or information messages;
- the exact current `persistence.py` source compiled in memory under project
  Python `3.14.3`, corroborating the PEP 758 thread disposition;
- `uv lock --check`: passed;
- `tools.skillgen --check`: all 134 artifacts matched;
- `tools.skillgen --audit-coverage`, `--schema-singleton`,
  `--monolith-roundtrip`, and `--always-on-roundtrip`: passed;
- `uv run --frozen pre-commit run --all-files`: passed `skillgen --check` and
  `ruff`;
- full repository suite: `5745 passed, 3 skipped, 5 warnings` in `1606.51s`;
- the explicit nine-file relative-link and heading-anchor audit passed with 166
  headings, 211 local link targets, and 46 heading-anchor targets;
- the documentation advisory audit completed with only expected
  source-verification prompts and action-run-number heuristics for the exact
  material claims already bound above; and
- the bounded anti-slop inventory found no placeholder, workaround, bypass,
  fallback, dead-code, or debug-marker addition. Repeated lifecycle wording is
  the maintained cross-document governance invariant, not duplicate alternate
  behavior.

Final whitespace, exact-manifest, independent-review, and patch-digest evidence
is reported at the local-diff boundary rather than embedded here, avoiding a
self-referential receipt digest.

## Accepted disposition

Only `P5B2 semantic-release decision-store and capacity/GC` transitions from
`IN_PROGRESS` to `COMPLETE` at this staged governance boundary. The trust-root
and policy-authority provisioning prerequisites remain `COMPLETE`. P5 and P5B2
remain `IN_PROGRESS`; the encompassing semantic-content release/DLP decision,
live operator-policy selection/provisioning, classification composition,
omission, projection, public surfaces, provider/backend, publication, remaining
P5B2 work, and P5C remain `WAITING`; H3 remains `DEFERRED`; no later successor
becomes `READY`.

No source, test, schema, fixture, dependency, lockfile, configuration, workflow,
generated Graphify output, runtime state, or GitHub review state is changed by
this staged receipt. Merge, cleanup, broader acceptance, and successor
activation remain separate reserved actions.
