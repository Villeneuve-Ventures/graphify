# S6 public workspace repair transport

This candidate adds `graphify workspace repair --request FILE` through the
existing canonical S5 request-file transport. It reuses
`PointerStore.analyze_repair()` and `recover()` with their candidate selection,
lineage, fencing, retained locks, and refusal rules. This slice authorizes no
live-state repair, provider work, offline GC, migration, legacy reader, D3
resolution, or S7 release qualification.

## Preview and approval

Use an already installed exact candidate and an explicit external state root.
Start the console with `PYTHONDONTWRITEBYTECODE=1`, or a trusted interpreter with
`-E -P -B -m graphify`. All workspace operations require non-elevated macOS on
local APFS; other platforms support help only. The S5 request and response
format versions remain 1. New helper and schema bytes participate in candidate
identity and admission. Old candidate authority cannot authorize these bytes.

Set `command` to `repair`. Preview parameters contain exactly:

```json
{"operation":"preview","repo_uuid":"11111111-1111-4111-8111-111111111111"}
```

The UUID must identify an explicitly enrolled disposable workspace. Successful
preview returns `result={"approval": <RepairApproval>}`. It reads existing
registry, workspace, journal, queue, pointer, receipt, and generation records
under retained locks. It allocates no lease and creates, repairs, removes, or
replaces no files, including temporary files. The worker's read-only audit is
installed before command imports. Missing lock identities, unresolved lease,
registry, semantic, staged-build, GC, or atomic temporary recovery state refuses;
preview does not recover such state. Existing pointer/journal evidence that the
core can repair remains eligible. Occupied leases refuse.

The approval object contains exactly these fields:

| Field | Binding |
| --- | --- |
| `repo_uuid` | Enrolled workspace UUID |
| `state_root_sha256` | SHA-256 of canonical JSON for the explicit state-root string |
| `compatibility_sha256` | Complete installed compatibility manifest identity |
| `active_source_sha256` | SHA-256 of canonical JSON for the registry's selected-source identity |
| `coordinates` | Exact registry revision, active-source revision, operation epoch, migration epoch, fence high watermark, candidate receipt source epoch, and expected result movement coordinates |
| `plan` | Complete bounded core repair plan, including `decision_sha256` |

Coordinates use `registry_revision`, `active_source_revision`, `operation_epoch`,
`migration_epoch`, `fence_high_watermark`, `source_epoch`,
`result_operation_epoch`, and `result_fence_token`. The result movement coordinates
are positive integers. Journal-only repair and pending finalization preserve the
existing pointer coordinates; replacement and pending resumption use the new
grant coordinates. Counters are JSON
integers through `2^63 - 1`; registry/source revisions and source epoch are
positive. Booleans, strings, and floats are not coerced.

The plan contains `classification`, `candidate`, `last_good`,
`next_pointer_revision`, `selected_from`, `pointer_action`, `journal_actions`,
`quarantine`, and `decision_sha256`. Generation references have exactly
`generation_id` and `receipt_sha256`. Generation IDs follow
`gen-[a-z0-9][a-z0-9._-]{0,62}`. Digests are lowercase SHA-256. The private raw
pointer and journal evidence stays inside the core; its decision digest is
public so approval retains the exact evidence binding. Paths and diagnostic
exception details are not included in the approval. The approval is a bounded
protocol record, not a signature or a new credential.

Preview analyzes the proposed next operation epoch and fence token, so pending
pointer ownership uses the same coordinates as the prospective REPAIR grant.
It refuses exhausted operation/fence counters and lease high watermarks that
precede durable journal history. `no_op` is a valid preview classification.

## Explicit execution

Execution parameters contain exactly `operation="execute"`, `repo_uuid`,
`authorization`, and `approval`. Copy the complete preview approval without
changes. Use the existing five-field operator assertion with
`action="REPAIR_EXECUTE"`: `action`, `operator_id`, `reason`, `issued_at`, and
`nonce`. The normal envelope carries the same explicit state root and compatible
candidate. The request and response schemas reject unknown fields and malformed
plans. The transport also checks that the parameter UUID equals the approval UUID.

Admission re-reads existing authority and re-runs analysis. Every approval field,
plan field, and decision digest must match before lease acquisition. A stale
approval returns the redacted `workspace_refused` response. There is no
replacement selection or automatic request rewrite. An approved `no_op` uses the same REPAIR lease and exact-approval checks.
It returns the existing pointer without replacing it; lease authority still
advances, so completed reuse of that approval refuses.

A repair requires a new valid `REPAIR` lease with the approved registry, source,
operation, and migration coordinates and the exact next fence. Core `recover()`
re-analyzes under registry, workspace, and exclusive generation locks and
compares the complete approved plan and decision digest before repair. Pointer
revisions stay monotonic. Generation selection, quarantine, journal recovery,
retained lineage, and pending-pointer rules remain the core contract. Source
content and unrelated state stay intact; retained locks are never replaced.

After lease acquisition, any failed repair, release, final authority check, or
completion acknowledgment reports `execution_unknown`. A killed or timed-out
worker cannot establish success or absence of effects. Preserve the original
request, approval, and durable lease/recovery evidence. The command does not
clear an uncertain lease or authorize successor recovery. Reusing a completed
approval refuses once its coordinates or evidence have changed. Obtaining a new
preview is an explicit operator step, never an automatic retry policy.

Existing S5 request limits, deadlines, response limits, redaction, and exit codes
apply. Preview timeouts report `deadline_exceeded`; uncertain execution reports
`execution_unknown`. Success returns `result={"pointer": <complete PointerSet>}`.

## Validation boundary

The task-owned branch starts from fetched `origin/v8` at
`da5df679dab18d2b8c150c01ebdfcaa55817ec7a`. The primary checkout's untracked
`.codex/config.toml` and all pre-existing worktrees remain outside this task.
Mutations are confined to disposable fixtures. Native power-loss and hostile
concurrent-rename qualification remain unestablished. Local validation results and limits are reported with draft-PR delivery.
They do not establish hosted CI or S7 qualification.
