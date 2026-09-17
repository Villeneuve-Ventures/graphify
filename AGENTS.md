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

Default to direct execution for coherent work with known validation. Use OMX
for the explicit requests, decision needs, and protected stages below; file
count or an accepted ordinary plan alone does not require orchestration.
Repository-state questions, review-only work, and unclear causes begin with
read-only evidence; stop at a verdict unless a patch is requested.

Before non-trivial work, choose the highest applicable lane:

- Use Deep Interview for material acceptance or operator decisions that remain
  unresolved after repo inspection and need structured clarification.
  Use Best Practice Research when official/upstream evidence materially affects
  an external API, format, security, or compatibility claim; research does not
  authorize edits.
- Use Ralplan for unresolved architecture/test-shape decisions, migrations,
  compatibility changes, or non-trivial protected changes. The
  protected-surface requirements below govern extraction, paths, manifests,
  publication, and workspace contracts even when the diff is small.
- Use Ultragoal for an explicit request, the protected requirement below, or a
  named checkpoint/resume need. Keep checkpoints in local `.omx/` artifacts.
  Use Team only when persistent shared ownership or durable runtime coordination
  justifies it; ordinary independent slices may use native subagents.
- After focused checks, use UltraQA only with explicit operator opt-in. Install,
  corpus, graph-integrity, recovery, and cross-platform risks identify candidate
  scenarios, not automatic activation. Start with one bounded cycle. Use Code
  Review for final independent review of protected, cross-cutting, or
  review-sensitive changes after relevant tests.
- Use Autopilot only for an explicit hands-off staged request; preserve its
  supervised interview, planning, and durable execution stages.

Reclassify when scope expands, a protected surface appears, validation fails
non-obviously, or review shows that the lane is mismatched.

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

Classify protected work by the behavior or contract effect, not the filename.
Name the affected invariant and why a change is non-trivial. Editorial
explanation of an unchanged invariant does not itself trigger implementation
orchestration.

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

Use focused checks during implementation. At final local PR validation, code,
test, configuration, cross-cutting, release-sensitive, or uncertain effects
require the canonical full suite below plus applicable surface checks. Follow
more-specific workspace contracts. Editorial docs require scoped whitespace,
link, and claim checks; contract docs require the checks covering their effects.
A completed check is reusable only while its inputs and applicable freshness
rules permit; a printed command or planned check is not completion evidence.

The repository requires Python 3.14 and uses the committed `uv.lock`; prefer
frozen commands matching CI.

- During implementation: run focused tests with
  `uv run --frozen pytest tests/<test_file>.py -q --tb=short`.
- Final full validation: run the canonical gate,
  `uv run --frozen pytest tests/ -q --tb=short -n 2 --dist=loadfile --max-worker-restart=0`.
  Use `uv run --frozen pytest tests/ -q --tb=short` for serial diagnostics or
  compatibility fallback. Protected changes also require the planning and
  independent review described above.
- Generated skills: run `uv run --frozen python -m tools.skillgen --check`,
  followed when applicable by `--audit-coverage`, `--schema-singleton`,
  `--monolith-roundtrip`, and `--always-on-roundtrip` exactly as defined in
  `.github/workflows/ci.yml`.
- CLI/install changes: run focused install/round-trip tests plus
  `uv run --frozen graphify --help`; use a disposable environment for any real
  install smoke test.
- Workspace-v1: follow `docs/workspace/v1/verification.md` and run focused
  workspace tests before the broader gate.
- Security or release artifacts: run the relevant Bandit and
  `tools.workspace_artifacts` build/audit commands from
  `.github/workflows/ci.yml`.
- Run configured Ruff or Pyright checks when they materially cover the changed
  surface; do not call them blocking CI gates unless CI says so.
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
5. Use `$code-review` for protected or cross-cutting changes. Block on supported
   in-scope defects; record other findings as rejected with evidence or
   non-blocking follow-ups. A priority label alone does not establish a defect.

After the required initial full-scope review, verify repairs and affected
dependencies; reopen discovery only for changed scope/contracts or concrete new
evidence. Preserve stronger review requirements in the governing protected
contract. Before another attempt, name the correction or evidence change. If
neither exists, diagnose the shared cause or report the bounded blocker instead
of launching another reviewer to seek a clean verdict. Planning-only work stops
at the plan; completing a milestone does not authorize its successors.

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

## Graph Orientation and Refresh Scope

This manual policy governs the installer-owned footer below. Use a retained
graph when it materially helps the requested lookup, starting with one bounded
scoped query. Additional queries must resolve a named remaining question. If
the graph is unavailable, stale, invalid, or unhelpful, preserve its output,
report the limitation, and use current source inspection. Do not rebuild or
repair graph infrastructure merely to answer a read-only question.

Batch required code-change refreshes on a stable candidate, before any proof
that depends on the refreshed graph. Refresh again only when changed inputs or
applicable freshness rules require it. Failure does not authorize force mode,
provider activation, or wider scope. Preserve mandatory workspace certification
and refresh contracts for tasks that actually depend on graph output.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
