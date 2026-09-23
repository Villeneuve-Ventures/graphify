# S3 lifecycle stores and storage separation

S3 is delivered as a five-PR stack ending at `codex/v8-workspace-lifecycle`,
based on v8 `7ecdef0859abfff9fb00469c7be4ce950c7815f7`. The donor was
inspected at `workspace/v1` commit `01ad5a3ca345da879c2f0c35161c4c185eb30d66`.
PR publication was separately authorized; this result does not authorize a
merge, state migration, or S4.

## Implemented boundaries

- The persistence, identity, registry, lease, journal, generation, pointer,
  semantic queue prerequisite, and internal GC stores are available behind
  explicit S2 structural composition. The composition constructs stores
  without creating state. Its operational adapter still refuses until S4.
- New roots receive a singular owned schema-2 marker before lifecycle
  descendants. An occupied unmarked root is not adopted. The marker denies
  ordinary writes; lifecycle writers still need their own descriptor,
  compatibility, operator, lock, and fenced-lease authority. Mutation requires
  non-elevated macOS on local APFS.
- Staged completion binds the S2 initial and consumed input manifests, exact
  compatibility tuple, and graph digest. Certification seals only
  `graphify-out/graph.json` and `graphify-out/input-manifest.json`, requires a
  queue binding, and records `semantic_completeness=not_required`. Pointer
  promotion and terminal recovery recheck the durable receipt and journal.
- Ordinary transaction, recovery, export, cache, and query-log entry points
  inspect the destination before output. Marked roots and recognizable old
  managed layouts are refused even through a symlink, before ordinary locks.
  The guard is denial-only and does not parse or migrate old state.

## Local checks

Run from this worktree with `uv sync --all-extras --frozen`:

```sh
uv run --frozen pytest tests/test_workspace_lifecycle_s3.py tests/test_workspace_storage_separation.py tests/test_workspace_composition.py -q --tb=short
uv run --frozen pytest tests/ -q --tb=short
uv run --frozen python -O -m pytest tests/test_protected_change_verifier.py -q --tb=short
uv run --frozen python -m tools.skillgen --check
uv run --frozen python -m tools.skillgen --audit-coverage
uv run --frozen python -m tools.skillgen --schema-singleton
uv run --frozen python -m tools.skillgen --monolith-roundtrip
uv run --frozen python -m tools.skillgen --always-on-roundtrip
```

After review repairs, the full serial gate passed **6,019 tests, with 41 skipped
and 235 subtests passed** (316.23 s). Focused lifecycle, storage separation,
export, and cache checks passed 132 tests. The optimized protected verifier
previously passed 80 tests and all five skillgen checks passed; their inputs
were unchanged by the review repairs. Ruff F checks on the new store/guard
modules and `git diff --check` passed. `uv build --wheel` produced an external
`graphifyy-0.10.0` wheel containing the repaired S3 modules and S2 root/input
schemas.

The native host probe reported non-elevated Darwin on APFS. The required
`graphify update .` AST pass completed with `GRAPHIFY_OUT` set to
`/private/tmp/graphify-s3-ast-review-SVglcD/graphify-out`; retained repository
graphs were not modified. The current CI help and install smoke commands both
passed with a disposable `HOME` at
`/private/tmp/graphify-s3-install-smoke-w01wj41l/home`; no real user
installation was changed.

## Independent review

The OMX code-review skill ran independent `code-reviewer` and `architect`
lanes over the initial 23-path S3 candidate, followed by focused correction
reviews of the repaired 24-path candidate.
Their reproduced findings led to exact consumed-input checks at completion
and first certification, a managed-subtree preflight for stale AST cache
cleanup, pinned-directory temporary export staging, ordinary-directory
binding rechecks, and cleanup of an owned staging file on failed admission.
The correction review returned `APPROVE` and architectural status `CLEAR`
with no remaining actionable finding in the reviewed repairs. It does not
establish the native proof listed below.

The focused fixtures cover linked-source activation/CAS, exact staged input
binding, stable queue and missing-binding refusal, capacity refusal, stale
fences, receipt/install/certified-state fault recovery, pointer promotion,
read-only GC preview, output refusal with preserved bytes/inodes/mtimes, and
injected platform limits. They use injected APFS capabilities and a synthetic
source observer; those fixtures are not native crash or adapter proof.

## Remaining boundary and S4

S4 still must supply the v8 adapter and structural orchestration, including
real S1 source observations, source drift handling, the engine build, and a
provider-neutral query round trip. This S3 diff does not expose workspace
commands or activate semantic providers. Legacy certified-state access remains
an explicit later decision. Native power-loss, hostile concurrent rename,
and complete S4 failpoint-matrix proof are not established by these fixtures.
