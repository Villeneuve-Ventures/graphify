# S5 public structural workspace surface

This candidate implements the S5 command, skill, and diagnostic boundary
from the [structural workspace design](graphify-v8-structural-workspace-design.md).
It exposes the existing S3 lifecycle and S4 adapter through explicit bounded
requests. S6 maintenance transports and S7 exact release qualification remain
separate. This record does not claim release qualification, live adoption,
migration, semantic completion, or CI passage.

## Candidate and authority

Use the exact installed candidate and its explicit runtime authority. The current
library tuple has distribution `graphifyy` version `0.10.0`, adapter contract **3**,
observer `graphify-v8/workspace-observer-v3`, input-manifest format **2**, scoped-input
ABI `graphify-v8-structural-2`, state schema **2**, and graph payload format **1**.
The complete compatibility manifest also binds candidate content; copying these
version numbers is not enough to establish authority. See the
[selected-ref contract](graphify-v8-selected-ref-contract.md).

Old manifests, staged requests, or receipts are not converted or re-signed. The
state root is explicit and external to the checkout. Mutation requires qualified
runtime/storage capabilities. Do not infer support from successful help or import.
A refusal does not grant permission to install authority, migrate, repair, or
change the selected source.

## Command and request contract

```bash
PYTHONDONTWRITEBYTECODE=1 graphify workspace --help
PYTHONDONTWRITEBYTECODE=1 graphify workspace register --request register.json
PYTHONDONTWRITEBYTECODE=1 graphify workspace activate --request activate.json
PYTHONDONTWRITEBYTECODE=1 graphify workspace sync --request sync.json
PYTHONDONTWRITEBYTECODE=1 graphify workspace query --request query.json
PYTHONDONTWRITEBYTECODE=1 graphify workspace status --request status.json
PYTHONDONTWRITEBYTECODE=1 graphify workspace doctor --request - < doctor.json
```

Use an already trusted candidate interpreter with `-B` when invoking
`python -m graphify`; set `PYTHONDONTWRITEBYTECODE=1` before console-script startup.
The canonical zero-write console launch sets `PYTHONDONTWRITEBYTECODE=1`
externally, as the examples above show. The module alternative is a trusted
Python with `-E -P -B -m graphify workspace ...`. Bare cold
console/module startup cannot guarantee zero writes: CPython can write the first
`graphify/__init__.py` bytecode cache before package code can disable caching.
S5 therefore requires external suppression before import; it adds no startup
`.pth` hook or installer mutation to change this interpreter behavior.
These transports bypass the ordinary skill bootstrap and its saved-interpreter,
scan-root, cache, learning-memory, and project-output writes.

`--request FILE` reads a request; `--request -` reads stdin. JSON must be UTF-8,
NFC-normalized, compact, sorted by key, with one final newline. Duplicate or unknown
keys, unsupported versions, malformed fields, and oversized inputs are refused.
The request limit is 1 MiB. Stdin acquisition has a separate 10-second limit; request files must be bounded regular files opened without following their final symlink. A serializer can produce canonical bytes with
`json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"`
after validating NFC text. Store request files outside protected source/state
roots when a scenario requires those roots to remain unchanged.

Every command uses the same exact envelope:

| Field | Required value |
| --- | --- |
| `contract` | `graphify.workspace.cli.request` |
| `format_version` | `1` |
| `command` | The selected subcommand |
| `request_id` | Matches `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` |
| `state_root` | Canonical absolute path to the explicit managed state root (no `..`) |
| `compatibility_manifest` | Full exact current candidate compatibility mapping |
| `timeout_ms` | Integer from 1 through 300000; covers the command's bounded work |
| `parameters` | Exact operation-specific mapping below |

Do not replace the compatibility manifest with a version string, invent epochs
from example values, or use a new request to bypass an uncertain prior result.

## Registration, activation, and structural sync

`register` takes `operation` (`enroll`, `adopt`, `rebind`, or `rotate`), absolute
`source_root`, `expected_registry_revision`, and `authorization`. Authorization
is the exact mapping `action`, `operator_id`, `reason`, `issued_at`, `nonce`.
Its action is the uppercase operation; the timestamp is RFC 3339 UTC. Each
operation retains the library's source-identity and policy checks. Enrollment
selects the initial source; adoption alone does not activate another source.
A clone with a colliding repository UUID is not silently adopted. Rebind and rotate
remain explicit operations, not error recovery defaults.

`activate` takes absolute `source_root`, `authorization` with action `ACTIVATE`,
`expected_registry_revision`, `expected_active_source_revision`,
`expected_operation_epoch`, `expected_migration_epoch`, and bounded `ttl_ns`
(up to 300 seconds). Activation must match the expected registry, active-source, operation, and migration epochs.
Same-source activation and stale compare-and-swap inputs refuse. Inspect the
current state and resolve the cause before submitting another operation.

`sync` has two request shapes:

| Operation | Additional parameters |
| --- | --- |
| `prepare` | `repo_uuid`, `generation_id`, `source_epoch`, `desired_watermark`, `expected_payload_bytes` |
| `execute` | `sync_request` (the exact frozen S4 request), `attempt_sha256` |

Prepare returns `sync_request`; preserve that full mapping for execution and
retry. Execution does not reconstruct it from the current checkout. Keep the
original attempt identity when acquisition or lease ownership is uncertain.
A newly chosen attempt digest is permitted only under the existing lifecycle
release/retry rules. A certified orphan does not prove successful promotion.
Success must reach the lifecycle's acknowledged pointer/promotion outcome.

Sync is structural-only. It accounts for observed source inputs and retains the
existing queue/watermark barriers, but does not run semantic extraction, a worker,
or a service. A structural `not_required` receipt is not semantic-complete proof.

## Query and read-only diagnostics

`query` takes `repo_uuid`, `question`, `mode` (`bfs` or `dfs`), `depth`,
`token_budget`, and `context_filters` (an array). The adapter validates its bounded
question, traversal, token, and filter contract before traversal. The question is
limited to 4096 UTF-8 bytes and 256 term units, depth to 8, and token budget to
32768 (positive); depth may be zero. Filters are bounded by the adapter to
16 entries, 128 UTF-8 bytes per entry, and 1024 bytes in total. The total response is bounded at 16 MiB. The command
buffers output until the existing certified generation, pointer, selected source,
authority, and before/after freshness checks permit release. A successful result
contains `text`. On refusal, no partial query text is printed and no ordinary
query fallback runs.

`status` and `doctor` take `repo_uuid`. Status reports whether existing state is
current, unbuilt, stale, pending, or unavailable and whether it is safe to query.
Doctor validates the current graph. Neither command repairs registry, pointer,
locks, queue, receipt, or generation state. Query and diagnostics do not enroll,
activate, sync, convert inputs, create logs or caches, or write project outputs.
Kernel advisory locks and access times are not durable application writes.

Freshness remains **observed-current**. It is not an atomic snapshot and does not
prove the absence of a change that was reverted between observations. Unrelated
packed-ref drift follows the selected-ref contract; selected routing or input
drift can withhold output even when the terminal HEAD OID is unchanged.

## Responses and refusals

Responses are canonical JSON with exact fields `contract`, `format_version`,
`command`, `request_id`, `outcome`, `result`, `error_code`. The contract is
`graphify.workspace.cli.response` and format version is `1`. Success uses
`outcome="ok"`, an operation-specific result, and `error_code=null` on stdout.
Refusal uses `outcome="refused"`, `result=null`, and a stable redacted error code
on stderr. Requests, credentials, local paths, and raw exception prose are not
echoed in error messages.

| Exit | Meaning |
| --- | --- |
| `0` | Successful command |
| `2` | Invalid or malformed request |
| `3` | Runtime or authority refusal |
| `4` | Lifecycle or freshness refusal |
| `5` | Bounded timeout or uncertain mutation outcome |

The supervisor enforces a wall-time limit and terminates the command process
group at expiry. A read-only timeout reports `deadline_exceeded`. A mutation
timeout reports `execution_unknown`: durable work may have occurred. Preserve
the exact frozen sync request and attempt identity for recovery; do not infer a
fresh retry from timeout alone.

Interpret status content as well as exit status. A successful diagnostic command
can report that a workspace cannot currently be queried. A refusal is not proof
of rollback or of no prior effects for a mutation; retain the frozen request and
inspect the bounded lifecycle disposition.

## Local validation boundary

The task started from freshly fetched `origin/v8` at
`68985c6bb74fe2898a10708af7eeb082d4f11799` in the isolated task worktree.
The primary checkout stayed clean. Existing issue105 worktrees were inspected
without edits. The initial delivery stopped at an uncommitted local diff.

The host is macOS **27.0**, build **26A428**, Python **3.14.3**, and Git
**2.55.0**. Installed lifecycle tests used native local APFS qualification.
Injected Linux, Windows, elevated, network, and non-APFS cases are portable
refusal fixtures; they do not establish native execution on those hosts.

The source skill changes live in
`tools/skillgen/fragments/core/{core,aider,devin}.md` and
`tools/skillgen/fragments/references/query/default.md`. The monolith guard
allows only the exact workspace routing line. Generated host skills and query
references were regenerated, and all five skillgen checks passed. The lean core
remains **799 lines**, below its existing 800-line guard; that guard was retained.
Always-on instruction files retain their existing contents.

The public transport/skill/package checks passed **460 tests**, with **39 skips**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen --all-extras pytest \
  tests/test_install_references.py tests/test_skillgen.py \
  tests/test_readme_policy.py tests/test_workspace_s5_cli.py \
  tests/test_workspace_s5_installed.py -q --tb=short
```

This includes a freshly installed candidate wheel, console and guarded module
entry points, source-identity registration/activation, exact sync retry,
CERTIFIED/PROMOTED receipt/journal/pointer bindings, before-import mutation
interception and byte/inode/mode/mtime snapshots, cold Chinese query cache
controls, unrelated packed-ref tolerance, selected-route refusal, missing-lock
diagnostics, and a hostile current directory containing a counterfeit package.
The shared dependency path file is test setup in a disposable environment;
no new startup path hook ships in the candidate.

The broader ordinary compatibility group passed **2785 tests**, with **39 skips**
and three existing jieba syntax warnings, except for one skill-length assertion.
The focused final transport/skill group above then passed that assertion after
the routing layout correction. The passing ordinary evidence covers CLI output
and pipes, install/upgrade/hooks, wheel packaging, transaction admission,
query scoring/tokenization, caches, structural extraction, resolver/language
coverage, clustering, and semantic-preservation behavior. No ordinary execution
code changed during the skill layout correction.

The workspace/contract/adapter regression group passed **273 tests** in
**16 minutes 53 seconds** (three existing jieba syntax warnings). It covered
S5 frontends/diagnostics/installed transports, canonical/schema contracts,
S4 installed cold queries, deadlines, worker imports, selected packed refs,
Git routing, admission/format controls, and prior review regressions:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen --all-extras pytest \
  tests/test_workspace_s5_cli.py tests/test_workspace_contracts.py \
  tests/test_workspace_s5_status.py tests/test_workspace_s5_installed.py \
  tests/test_workspace_s4_installed.py tests/test_workspace_s4_deadlines.py \
  tests/test_workspace_s4_worker_imports.py tests/test_workspace_packed_refs.py \
  tests/test_workspace_s4_git_routing.py tests/test_workspace_s4_admission.py \
  tests/test_workspace_s4_admission_formats.py tests/test_workspace_s4_review.py \
  -q --tb=short
```

The **20 durable-boundary exact-retry tests** passed in **6 minutes 40 seconds**:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen --all-extras pytest \
  tests/test_workspace_structural_s4.py -k exact_retry_at_durable_boundaries \
  -q --tb=short
```

The ordinary group took **4 minutes 40 seconds**; the final transport/skill
checks took **2 minutes 10 seconds**. A bounded process snapshot during the
longer workspace/recovery groups showed ongoing Python execution and CPU
accumulation. It does not establish a precise per-function bottleneck or a
performance improvement. Across these overlapping groups, **3080 distinct
test cases** passed after the size guard correction; **39 skipped cases** remain
explicit gaps. The full repository pytest/release gate was not run for this
local-diff slice.

A final canonical-wire correction uses binary standard streams so unsupported-host
refusals retain UTF-8/LF bytes without platform newline translation. The focused
S5 frontend/installed checks passed **51 tests** in **70 seconds** after that
change, including a new binary-wire regression and a fresh installed round trip.
Graphify was refreshed again after this final code change.

The independent read-only review found one request-schema discrepancy: the
library permits fractional UTC seconds in authorization timestamps. The schema
was corrected and whole registration/activation model/schema parity checks were
added. No other supported production findings were reported. Scoped Ruff and
`git diff --check` passed.

`graphify update .` completed through a freshly discovered trusted task
interpreter after code edits. The final AST graph contains **16011 nodes**,
**40169 edges**, and **945 communities**, with zero API tokens. Six pre-existing
fixture/data inputs yielded no nodes; HTML rendering was skipped by the existing
5000-node cap. These graph observations are orientation evidence, not workspace
certification.

## Acceptance coverage and remaining limits

| Matrix | S5 evidence |
| --- | --- |
| A1 | Installed enroll/adopt/activate/rebind/rotate, stale CAS and clone/same-source refusals, certified structural round trip, and known relation query. |
| A2 | Installed selected-ref positive/negative controls and diagnostics drift; existing S4 source/consumed-input freshness checks remain the responsible library boundary. |
| A3–A4 | Existing admission/format/worker/routing checks plus public request bounds, no provider dispatch, cache controls, isolated computation, and hostile-CWD launch. |
| A5 | Installed exact promoted retry, timeout process-group cleanup/unknown mutation disposition, and existing durable-boundary recovery tests. |
| A6 | Public old-tuple/wrong-candidate refusal, explicit installed authority, registry CAS, and existing admission/epoch/queue barriers. |
| A7 | Ordinary transaction/output/cache/hook regression checks; no public maintenance or output fallback added. |
| A8 | Installed query/status/doctor success/failure mutation audit and snapshots, buffered output, bounded traversal and workers, and absent/misleading tokenizer caches. |
| A9 | Native APFS round trip, injected unsupported-host refusals before lifecycle writes, unavailable primitive guard, ordinary lazy help/install compatibility. |
| A10 | Existing engine/scoring/tokenization/language/cache/clustering regressions plus installed known-relation and Chinese queries. |

These are scoped local S5 checks. They do not claim every A1–A11 scenario was
rerun through every public transport, native Linux/Windows support, an atomic
source snapshot, or hidden-ABA detection. S6 maintenance transport acceptance
and S7 full aggregate/exact release qualification remain separate. The old
certified-state decision remains outside this slice. No live root was enrolled,
activated, migrated, repaired, or collected. During the initial local-diff slice,
no task commit, push, PR, or CI wait was performed.

## Pre-PR validation

After the operator authorized PR delivery, aggregate validation exposed two
legacy fixture inventories that omitted the three newly required S5 modules.
`tests/test_workspace_fixture_inputs.py` and
`tests/test_workspace_installed_identity.py` now include `workspace/cli.py`,
`workspace/cli_contracts.py`, and `workspace/status.py`. The production package
member checks remain intact. The first aggregate run was interrupted after the
common fixture failure was reproduced; it is failed partial evidence, not a
passing gate. The corrected fixture/build/installed-identity checks passed
**75 tests and 137 subtests**.

The complete repository suite then passed through all four maintained CI
partitions. Every shard performed full `tests/` collection. Its completed timing
receipt had exit code zero, validated partition selection, and matching source,
tracked changes, allocation, and configuration hashes. The four selected
inventories are disjoint and their exact union contains all **7699 collected
cases**: **7658 passed, 41 skipped, and 235 subtests passed**.

The shard command was:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen --all-extras python -m pytest \
  -p tools.pytest_partition -p tools.pytest_timings --ci-shard=N \
  --ci-timing-json /absolute/local/shard-N.json tests/ -q --tb=short
```

Shards 1 and 4 used separate explicit temporary directories. On this Mac,
`uv run python` selects the `python3` alias while the normal pytest launcher and
skill bootstrap use `.venv/bin/python`. Five lexical launcher-path assertions
failed under the alias and passed under the normal launcher. Shard 3 was rerun
in full using `uv run --frozen --all-extras .venv/bin/python -m pytest` with the
same partition/observer options. Shard 2 was also rerun in full with that
interpreter and pytest's normal host temporary directory: the custom `/tmp`
directory inherits group `wheel`, causing macOS to strip a hook fixture's
requested setgid bit before installation. A minimal mode control reproduced
the difference, and the hook case passed in the normal user temp directory.
These controls changed local invocation only; test assertions and gates were
retained. Passing evidence from the other two shards was retained.

The successful shard durations were **22 minutes 30 seconds**, **19 minutes
7 seconds**, **13 minutes 13 seconds**, and **14 minutes 55 seconds**. The
slowest shard's timing receipt attributes **14 minutes 49 seconds** to
`tests/test_workspace_structural_s4.py`. These measurements do not claim a
sustained speed improvement. The existing jieba syntax, out-of-scope semantic
cache, and managed-state cache-cleanup warnings remain disclosed.

The remaining local quality checks passed:

- `uv run --frozen python -O -m pytest tests/test_protected_change_verifier.py -q --tb=short`: **80 passed**, with pytest's expected optimized-Python assertion warning.
- `uv run --frozen python -m tools.ci_pytest_gate smoke`: isolated ordinary help and installation passed.
- Scoped Ruff, including the corrected fixture inventories, and `git diff --check` passed.
- The five generated-skill checks from the initial slice remain current; their inputs were unchanged by the fixture corrections.
- `graphify update .` passed after the fixture edits, with the same **16011 nodes**, **40169 edges**, **945 communities**, and zero API tokens.

This is complete local repository test coverage, not observed remote CI or S7
exact release qualification. The 41 skipped cases and native Linux/Windows
qualification remain explicit limits. S6/S7, live adoption, migration, and CI
monitoring remain outside this PR delivery.
