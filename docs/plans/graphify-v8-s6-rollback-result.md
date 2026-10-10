# S6 public rollback transport

This slice adds `graphify workspace rollback --request FILE` to the bounded
workspace transport. It calls the existing `PointerStore.rollback` and does
not expose repair, GC, arbitrary historical selection, or a legacy reader.
S6 repair/GC and S7 remain incomplete. This record does not authorize real-state
maintenance, migration, release, installation, or successor implementation.

## Request and result

Use the exact installed candidate, explicit external state root, and the
[S5 canonical request envelope](graphify-v8-s5-result.md#command-and-request-contract).
The request/response format stays at version 1. Candidate content identity changes;
old authority does not authorize the new helper. Start the console with
`PYTHONDONTWRITEBYTECODE=1`, or a trusted interpreter with
`-E -P -B -m graphify workspace rollback --request FILE`.
All workspace operations require non-elevated macOS on local APFS.

Set `command` to `rollback`. `parameters` contains exactly these fields:

| Field | Meaning |
| --- | --- |
| `repo_uuid` | Canonical UUID of the explicitly enrolled workspace |
| `authorization` | Exact S5 authorization mapping, with action `ROLLBACK` |
| `expected_registry_revision` | Current registry revision, positive integer |
| `expected_active_source_revision` | Current selected-source revision, positive integer |
| `expected_operation_epoch` | Current lease operation epoch, nonnegative integer |
| `expected_migration_epoch` | Current migration epoch, nonnegative integer |
| `expected_fence_high_watermark` | Current lease fence high watermark, nonnegative integer |
| `expected_pointer_revision` | Current pointer revision, positive integer |
| `expected_current_receipt_sha256` | Current pointer's lowercase SHA-256 receipt identity |
| `expected_source_epoch` | Exact target receipt's source epoch, positive integer |
| `target_generation_id` | Exact `last_good` generation ID |
| `target_receipt_sha256` | Exact `last_good` receipt's lowercase SHA-256 identity |

Request counters are JSON integers through `2^63 - 1`; booleans, strings, and floats are
not coerced. Generation IDs follow `gen-[a-z0-9][a-z0-9._-]{0,62}`.
Authorization follows the existing operator assertion contract: it is not a new
credential, signature, or permission system. Do not invent revisions from this
record. Bind a request to current verified pointer, receipt, registry, and lease
records. The target source epoch names the target receipt, which may differ from
the current generation's source epoch.

Before lease allocation, admission reads existing records under registry,
workspace, and generation locks. It checks source identity, candidate authority,
all expected coordinates, exact `last_good`, complete receipt/payload verification,
and certified journal authority. Missing, corrupt, incompatible, or wrong-target
state refuses. Pending registry/lease/journal/pointer/staged/GC recovery, retained
semantic recovery state, or an occupied lease also refuses. Admission does not
repair those records. The stable semantic queue is read under the workspace lock;
retained items, semantic-required reconciliation even after item compaction, and
unreadable or pending queue records refuse before lease allocation and replay.
Lease acquisition repeats the existing registry/epoch,
source, recovery, and contention checks. Pointer movement repeats the existing
CAS, lease, fence, compatibility, journal, and generation checks.
When the current generation is verifiable, its receipt must agree with the visible
pointer's active-source revision and source epoch before lease allocation. A
metadata mismatch refuses without repair. This does not require the current
generation's source epoch to equal the target generation's source epoch.

Success returns `result={"pointer": <complete PointerSet>}`. The pointer revision
advances once; the current reference is the requested exact `last_good`, and the
journal records `ROLLED_BACK`. The lease operation epoch and fence advance by one.
Valid generation payloads and source content remain unchanged. The core store's
existing handling of a corrupt previous current generation remains in force.
Rollback does not assert current source freshness or semantic completion; query,
status, and doctor retain their existing read-only freshness checks.

A completed exact retry returns the recorded pointer without durable writes.
It requires the original movement coordinates, verified target, retained prior
pointer, matching `ROLLED_BACK` journal event, unchanged registry/source/migration
authority, no intervening operation/fence advance, and no active lease or recovery
intent. Request ID and authorization prose are transport metadata; replay identity
is the complete movement tuple. A changed target or stale tuple cannot toggle the
pointer. A later deliberate rollback needs a new current tuple.
The retained pointer's current generation reference, operation epoch, and fence
must match its own durable pointer journal event. Its active-source revision and
source epoch must match its fully verified receipt. These are the prior generation's
coordinates, which can differ from the rollback target's source epoch and from the
request's later lease high watermarks. If rollback quarantined a corrupt prior
generation, the move can succeed, but completed retry refuses when that prior
receipt can no longer establish the complete retained evidence. It preserves the
completed pointer and recovery records for separately authorized inspection.

## Uncertainty and preservation

Request acquisition limits, the command deadline, isolated worker imports,
response size limits, redacted errors, and exit codes remain the S5 contract.
An interrupted mutation, timeout, malformed worker response, or failed completion
acknowledgment reports `execution_unknown`. Every failure after lease
acquisition reports `execution_unknown`, including pointer movement, lease release,
and final authority checks; acquisition already advances durable operation and
fence authority. Worker termination cannot prove that
rollback completed or that no prior effects occurred. Preserve the request and
durable lease/intent evidence. Pending recovery refuses exact retry until the
existing separately authorized lifecycle recovery resolves it; this command does
not acquire successor recovery authority or clear an uncertain lease.

## Validation boundary

The task-owned worktree starts from fetched `origin/v8` at
`a13c9057f5c8b17d7b7f80bffcb4e38848e9ff84`. Selection baseline
`85f8ab4a1ea87a19e31156483426949962169906` advanced through PR #203 only
(README and PR-Agent routing). The three selection fingerprints still matched.
Those merged paths stay outside this slice. The reference checkout's untracked
`.codex/` and all occupied worktrees remain untouched.

Host inspection: macOS **27.0**, build **26A428**, Darwin **27.0.0**, arm64,
UID **501**, CPython **3.14.3** built with Clang **22.1.1**, Git **2.55.0**,
and uv **0.11.30**. Native capability detection reports local **APFS** with
`elevated=False`. Tests mutate only task-owned disposable source/state roots.
Portable injected fixtures do not establish native execution or power-loss proof.

## Local validation

The prior repair candidate `32c590a8` has complete saved receipts. This section
records that candidate; later repair validation is recorded separately below.
Frozen all-extra setup (`uv sync --all-extras --frozen`) checked 171 packages.
The aggregate used the repository's four serial pytest shard entry points:
`PYTHONDONTWRITEBYTECODE=1 uv run --frozen python -m pytest
-p tools.pytest_partition -p tools.pytest_timings --ci-shard=N
--ci-timing-json RECEIPT tests/ -q --tb=short --basetemp=FIXTURE_ROOT/N`.
The disposable fixture parent was private, owned by UID 501/group 20, and outside
a directory named `worktrees`. Each shard completed with exit status zero.

| Shard | Passed | Skipped | Passed subtests | Observed duration |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2,393 | 7 | 104 | 2,340.75 s |
| 2 | 1,707 | 0 | 47 | 1,708.67 s |
| 3 | 2,350 | 38 | 63 | 1,136.72 s |
| 4 | 2,648 | 2 | 21 | 1,222.78 s |
| Total | **9,098** | **47** | **235** | |

The four timing receipts have the same 9,145-test inventory and allocation hash.
Their selected inventories are disjoint and cover that complete inventory.
The parallel aggregate took about 39 minutes. The largest observed
modules were `test_workspace_structural_s4.py` (821.74 s) and
`test_workspace_s6_rollback.py` (819.45 s), including setup and teardown.

That run includes the six pointer-source/post-acquisition regressions,
canonical/schema agreement, stale authority and target
refusals, complete exact retry, recovery/GC barriers, occupied leases, interruption,
ambiguous worker output, and preserved source/generation/unrelated content.
The cold installed S6 test uses real candidate admission and the bounded console
and module workers on native macOS/APFS. It covers helper membership and tamper
refusal, rollback/journal output, write-free completed retry, and audited read-only
query/status/doctor behavior after rollback.

Additional passing checks:

- Ruff over `graphify` and `tests`, expanded focused F/E9 checks, and focused
  Pyright on the rollback helper (zero errors and warnings).
- All five generated-skill validators: `--check` (134 artifacts),
  `--audit-coverage`, `--schema-singleton`, `--monolith-roundtrip`, and
  `--always-on-roundtrip`. Existing managed-command routing needs no regenerated
  skill changes.
- Optimized protected-verifier tests: 80 passed; the expected Python `-O`
  assertion warning remains. The disposable CLI/install smoke check passed.
- Native Leiden smoke checks, including an isolated binary-only installation of
  the published Leiden extra.
- Required `graphify update .` through the supported runtime, after the final
  code/test edit: AST-only graph refresh completed. The six zero-node source
  warnings and the large-graph HTML skip remain diagnostic output.
- `git diff --check`; workflow, lockfile, and package-configuration hashes stayed
  unchanged during validation.

Both repository security receipts completed. Bandit reported **11 findings** in
unchanged paths; every scanned production hash still matches. This is an advisory
findings result. The dependency audit reported **zero findings** for the frozen
CI default/dev inventory. The first all-extra dependency audit could not satisfy
that scanner's default/dev inventory contract; the complete receipt comes from a
separate disposable environment with that exact scope.

Earlier aggregate attempts exposed a missing helper in the shared synthetic
fixture, direct-interpreter bootstrap context, and temporary-root path/group
assumptions. The fixture now includes the required helper. Focused reproductions
passed before the complete final run through `uv run --frozen` in a private
user-group fixture root. Earlier logs and incomplete receipts remain preserved
outside the repository and do not establish gate passage.

S7 exact release qualification, native power-loss and hostile concurrent-rename
proof, D3, real adoption, and hosted CI remain outside this delivery.
CI not awaited.

## Replay and semantic queue repair validation

The subsequent repair binds the retained pointer's complete authority and reads
the stable semantic queue before fresh admission or replay. Eleven focused
refusal cases failed against `32c590a8` before these changes; all 16 selected
behavior checks passed after repair. A separate two-case check passed with and
without a released lease advancing the pre-rollback high watermarks. The cold
installed worker and README policy checks passed (138 tests), including pending
queue and altered retained-fence refusals with preserved state.

New complete receipts validate the repaired code and tests through the same
four-shard entry points and private fixture parent contract:

| Shard | Passed | Skipped | Passed subtests | Observed duration |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2,404 | 7 | 104 | 2,821.94 s |
| 2 | 1,707 | 0 | 47 | 1,760.32 s |
| 3 | 2,350 | 38 | 63 | 1,188.09 s |
| 4 | 2,648 | 2 | 21 | 1,285.63 s |
| Total | **9,109** | **47** | **235** | |

The four complete receipts share the 9,156-test inventory and allocation hash.
Their disjoint selections cover the full inventory with exit status zero.
The aggregate took about 47 minutes. The largest modules were
`test_workspace_s6_rollback.py` (1,249.18 s) and
`test_workspace_structural_s4.py` (819.01 s), including setup and teardown.

Frozen all-extra setup, Ruff, focused Pyright, all five generated-skill validators,
80 optimized verifier tests, CLI/install smoke, native Leiden smoke, and the
required AST-only graph update passed. Code/test and workflow/lock/configuration
hashes stayed unchanged through validation. Existing docstrings stayed unchanged.
Both new security receipts completed: Bandit reported the same 11 findings in
unchanged paths; the frozen default/dev dependency audit reported zero findings.
The historical receipt correction above reused the valid `32c590a8` evidence;
these new shards qualify the code repairs. CI and bot reviews not awaited.
