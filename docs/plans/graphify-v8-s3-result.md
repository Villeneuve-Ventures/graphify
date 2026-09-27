# S3 lifecycle stores and storage separation

The initial S3 candidate was assembled as a five-PR stack ending at
`codex/v8-workspace-lifecycle`, based on v8
`7ecdef0859abfff9fb00469c7be4ce950c7815f7`. This integration PR is
synchronized onto v8 `4227a4bc48ac5126814eb6360cc59687dc7e11d3` after
PR #165. The donor was inspected at `workspace/v1` commit
`01ad5a3ca345da879c2f0c35161c4c185eb30d66`.
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

### Historical receipts for earlier S3 candidates

The following receipts predate the September 26 acceptance candidate. They do
not cover the later pointer, GC, and capacity repairs. The revision-bound
receipt below supersedes them for those repairs.

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

### September 26 acceptance receipt

The tested source and test contents were committed as
`d4329ea88d957f6f2a1c746219b27723a416787f`, based on v8
`4227a4bc48ac5126814eb6360cc59687dc7e11d3`. The serial run finished before
commit creation; all 13 changed-file hashes were then checked against the
committed contents, and the worktree was clean. The review manifest's SHA-256
was `6ff0494cb3c15f4095521a25a406b0845338e7d3fe0e0c5df3bf4490bce322c8`.
Raw logs and the manifest remain session-local; the results below are the
repository-visible receipt, not a claim that those artifacts are committed.

The environment was prepared with `uv sync --all-extras --frozen`, using
CPython 3.14.3 and Git 2.55.0. The final full-suite invocation was:

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/ -q --tb=short -p no:cacheprovider
```

It passed **6,618 tests, with 41 skipped and 235 subtests passed**, in
**792.03 seconds**. Six warnings concerned existing jieba escape sequences
and expected cache-safety refusals. This run includes the capacity and pointer
acceptance suites and the final GC recovery regressions. An earlier run with
missing extras and a different interpreter entry point failed; it is not the
acceptance run.

The optimized-Python verifier passed **80 tests** in 10.14 seconds. All five
skillgen commands listed above passed, as did Ruff F checks on the seven
repair/test paths, `git diff --check`, and `graphify --help`. The AST-only graph
refresh completed after the final source edits. A new wheel build, installation
smoke, and native crash test were not part of this acceptance run.

Independent code-review and architecture lanes reviewed the complete 13-path
candidate and the final correction round. They returned **APPROVE** and
**CLEAR**, with no remaining substantiated P0–P2 finding. CI was not awaited.
This receipt is bound to the commit above; this later documentation-only
clarification does not claim another full-suite run or widen the S3 boundary.

### Post-receipt generation recovery correction

The documentation-only clarification above was committed as
`318a6858bb0bd0589803c2121347df2da7c719a8`. A subsequent production change,
`26905efef580010cf516c3bd3cf047c4672624e1`, modified
`graphify/workspace/generations.py` and
`tests/test_workspace_capacity_acceptance.py`. The earlier 6,618-test run and
13-path independent review do **not** cover this later change.

The correction lets stale certification recovery finish an already-sealed
staged or installed receipt under its original reservation after the configured
payload limit is lowered. Receipt, payload, request, queue, journal, and fence
validation remain in place. Admission, allocation, direct certification, and
recovery without a receipt retain their current-limit checks.

The final source and test contents were validated before commit creation and
matched against the committed files. These focused commands passed:

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_workspace_capacity_acceptance.py -q --tb=short -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_workspace_generation_io_limits.py tests/test_workspace_generation_inventory_review.py tests/test_workspace_lifecycle_s3.py -q --tb=short -k 'certif or receipt or successor_fence or capacity' -p no:cacheprovider
```

The first passed **15 tests** in 25.01 seconds; the second passed **13 tests**,
with 61 deselected, in 20.42 seconds. Coverage includes receipt-durable,
installed-generation, and journal-CERTIFIED interruption points; rejection of
damaged receipts or payloads with preserved state; repeated recovery with an
unchanged receipt and one CERTIFIED event; and subsequent promotion-lease
acquisition. The existing no-receipt limit-refusal case remains covered.

Ruff F checks, AST parsing, and `git diff --check` passed. The AST-only graph
refresh completed. Independent code review returned **APPROVE** and architecture
review returned **CLEAR** for this two-file correction. Supporting results are
session-local; this section records their revision and scope in the repository.
The full suite and other aggregate gates from the earlier receipt were **not
rerun** for this correction. CI was not awaited. Native power-loss proof and S4
remain outside these results.

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
