# Graphify Agent Contract

## Scope And Authority

This file is the repo-local operating contract for Graphify. A more specific
`AGENTS.md` or `AGENTS.override.md` takes precedence for its subtree.

Use live repository state as authority. In particular:

- `pyproject.toml` owns package metadata, supported Python, dependencies, and
  tool configuration.
- `.github/workflows/ci.yml` owns the blocking CI command shapes.
- `README.md` owns public installation and CLI guidance.
- `graphify/workspace/schemas/` and `docs/workspace/v1/` own the workspace-v1
  schema and governance contracts; implementation and tests are conformance
  evidence, not permission to silently weaken those contracts.
- Current code and tests own descriptive implementation behavior.

Follow the global OpenAI Developer Docs and Context7 lookup rules. Prefer live
CLI help, exact code, tests, schemas, and CI over retained prose for
version-sensitive behavior.

## Operating Posture

Graphify is a public, cross-platform developer tool that reads user-selected
corpora and writes generated graph state. Preserve these defaults:

- deterministic and offline-capable behavior where the existing contract
  provides it;
- explicit provenance (`EXTRACTED`, `INFERRED`, or `AMBIGUOUS`) rather than
  invented certainty;
- safe path, symlink, cache, manifest, and output-root handling;
- backward-compatible public CLI, package, schema, and installed-skill
  behavior unless the task explicitly authorizes a breaking change;
- no secret values in output and no ambient provider, database, or network use
  merely to make a test or workflow pass.

Do not rename the `graphifyy` package or the `graphify` / `graphify-mcp` CLI
entry points without explicit operator approval.

## Workflow Precedence

OMX is Graphify's primary workflow surface for non-trivial planning,
coordination, durable execution, adversarial QA, and review. Direct Codex work
remains appropriate for small, coherent changes with obvious validation and no
protected or cross-contract surface.

Choose the highest applicable lane before editing and reclassify if scope
expands, a protected surface appears, validation fails non-obviously, or review
shows that the lane is mismatched:

- Direct Codex: small, single-surface changes with an obvious focused test.
- Evidence/read-only: repository-state questions, review-only work, or unclear
  causes. Use normal repository inspection or `$analyze` for grounded
  cross-file synthesis; stop at a verdict unless a patch is requested.
- `$deep-interview`: materially ambiguous goals, missing acceptance criteria,
  explicit "do not assume" instructions, or operator choices that repository
  evidence cannot answer.
- `$best-practice-research`: current official or upstream evidence when an
  external API, SDK, format, security rule, or compatibility claim materially
  affects the plan. It does not authorize edits.
- `$ralplan`: multi-file behavior, architecture or test-shape uncertainty,
  migrations, compatibility changes, or any non-trivial protected-surface
  change that needs a reviewed implementation plan.
- `$ultragoal`: the default durable execution handoff after an accepted plan,
  with goal state and checkpoints kept in local `.omx/` artifacts.
- `$team`: coordinated parallel execution only when independent lanes,
  worktrees, shared task state, or durable tmux coordination justify the
  overhead. Team is an explicit tmux/runtime surface, not the default for a
  small patch.
- `$ultraqa`: hostile end-to-end validation when install/uninstall behavior,
  corpus boundaries, graph integrity, recovery, or cross-platform behavior
  needs adversarial scenarios beyond ordinary focused tests.
- `$code-review`: final independent review for protected, cross-cutting, or
  review-sensitive changes after the relevant tests pass.
- `$autopilot`: only when the operator explicitly requests hands-off staged
  orchestration; preserve its supervised interview, planning, and durable
  execution stages.

When routing work through OMX, use only workflows that are active in the
currently installed OMX catalog, and use live `omx --help` when workflow
availability or command syntax matters.

Use normal Codex repository tools for ordinary inspection. Use
`omx sparkshell -- <command>` only for explicit shell-native read-only evidence
and `omx sparkshell --tmux-pane <pane-id>` only for bounded tmux inspection.
Outside an active Team run, native subagents may handle bounded independent
research, implementation, review, or verification slices when doing so
materially improves quality or speed; the leader owns integration and final
verification.

## Protected Surfaces

Treat the following as protected or cross-contract work:

- extraction semantics, provenance, node or edge identity, deduplication, and
  graph integrity;
- incremental update, cache, manifest, shrink-guard, pruning, and generated
  output behavior;
- path traversal, symlink resolution, command execution, hooks, installer and
  uninstaller behavior, provider credentials, database access, and network
  boundaries;
- public CLI flags or output, package/install layout, host integrations,
  schemas, serialized state, and compatibility guarantees;
- workspace-v1 persistence, journal, pointers, generations, composition,
  semantic handoff/release, rollback, garbage collection, policy authority,
  and failure-atomicity contracts.

For a non-trivial protected-surface change, inspect the governing contract and
tests first, use `$ralplan` before implementation, and execute an accepted plan
through `$ultragoal`. Add or reproduce a failing regression before changing bug
behavior when feasible. Never weaken an integrity, provenance, security,
failure-atomicity, or compatibility guarantee merely to clear a test.

## Change Discipline

- Read the touched implementation, tests, schemas, and authority docs before
  editing.
- Keep changes narrow and preserve public behavior not named by the task.
- Prefer existing helpers and the standard library; do not add dependencies
  without explicit need and approval.
- Preserve optional-extra boundaries. A default install must not begin
  importing an optional dependency unconditionally.
- Treat files rendered by `tools/skillgen/` as generated. Change their owning
  fragments, regenerate them, and run the skill-generation checks rather than
  hand-editing rendered skill artifacts.
- Test installer and hook mutations against disposable homes/projects. Do not
  use a real user configuration as a smoke-test target.
- Do not commit generated `graphify-out/`, local `.omx/`, `.codex/`, cache,
  temporary, or environment-specific artifacts as repository proof.

The final `## graphify` section in this file is installer-owned. Keep manual
Graphify/OMX policy above that heading so `graphify codex install` can refresh
its own section without deleting hand-authored repository instructions.

## Validation

Use the smallest check that proves the changed claim, then expand in proportion
to risk. The repository requires Python 3.14 and uses the committed `uv.lock`;
prefer frozen commands matching CI.

- Focused tests:
  `uv run --frozen pytest tests/<test_file>.py -q --tb=short`
- Full test gate for cross-cutting or release-sensitive changes:
  `uv run --frozen pytest tests/ -q --tb=short`
- Generated skill changes:
  `uv run --frozen python -m tools.skillgen --check`, followed when applicable
  by `--audit-coverage`, `--schema-singleton`, `--monolith-roundtrip`, and
  `--always-on-roundtrip` exactly as defined in `.github/workflows/ci.yml`.
- CLI/install changes: run focused install/round-trip tests plus
  `uv run --frozen graphify --help`; perform any real install smoke test only
  in a disposable environment.
- Security or release-artifact changes: run the relevant Bandit and
  `tools.workspace_artifacts` build/audit commands from
  `.github/workflows/ci.yml`.
- Workspace-v1 changes: follow `docs/workspace/v1/verification.md` and run the
  focused workspace tests before the broader gate.
- Run configured Ruff or Pyright checks when they materially cover the changed
  surface; do not present them as blocking CI gates unless CI says so.
- Always run `git diff --check` and inspect the final diff for scope.

Graphify has no repo-local `make review-ready` contract. Do not import that
command or Aletheia/mac-mini-specific acknowledgement fields. A clean OMX
review complements but does not replace Graphify's focused tests and applicable
CI-parity checks.

## Pull Request And Reporting Boundary

For PR review, issue-fix, or review-comment work that edits files:

1. Resolve the exact issue/PR scope and revision before editing.
2. Patch only justified in-scope behavior.
3. Run focused validation, then the applicable CI-parity checks.
4. Review the final diff against the requested scope.
5. Use `$code-review` for protected or cross-cutting changes and report any
   unresolved findings as blockers.

Local edit authority does not imply commit, push, PR mutation, review
submission, merge, or cleanup authority. Final reports must name the exact
changed files, tests and gates run, failures or validation gaps, and whether a
post-code-change graph refresh was required and completed.

## Local Runtime State

- `graphify-out/` is generated orientation and query state. Bind important
  findings back to the current revision, dirty state, code, tests, and schemas
  before treating them as proof.
- `.omx/` stores local workflow state, plans, logs, and durable execution
  checkpoints. It is not committed proof.
- `omx wiki` is supplementary project memory for durable, public-safe
  architecture and debugging knowledge; it does not replace maintained source,
  tests, schemas, or PR-visible evidence.
- Attached tmux is required only by a selected OMX runtime such as Team, by an
  explicit operator request, or by a future repo gate that says so. Do not
  invent an attached-tmux readiness requirement for ordinary Graphify changes.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
