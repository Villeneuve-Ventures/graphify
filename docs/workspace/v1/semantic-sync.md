# Host-agent semantic-worker contract

This document freezes one contract-only P5B2 child: the future public
host-agent semantic-worker transport. The child is `READY` for a separately
authorized implementation review; it is not implemented, accepted, or
authorized by this document. Full semantic sync remains waiting.

The boundary reuses the existing `SemanticQueueStore`, `SEMANTIC_CLAIM` lease
domain, installed runtime authority, active-source policy, external durable
state root, retry/dead-letter rules, and semantic fragment sanitizer. It adds no
provider discovery, network backend, queue state-machine revision, generation
certification, promotion, or pointer authority.

## Exact public transport

The only future argv is:

```text
graphify workspace semantic-worker --stdio
```

Any other, reordered, repeated, or extended argv exits 64 with deterministic
usage text before installed-authority loading, standard-input reads, source
discovery, lease allocation, or state access. The valid form loads and composes
the installed `runtime-manifest.json` authority before it reads the first
protocol frame.

The command is one long-lived process for exactly one queue item. It derives
the trusted boot, PID, and process-start owner, acquires one
`SEMANTIC_CLAIM` lease, and retains that same owner and fence through claim,
optional checkpoints, terminal completion or failure, and release. Separate
phase-specific subprocesses are forbidden because they cannot share the exact
lease owner.

Standard input and standard output carry canonical UTF-8 JSON Lines. Each
frame is one duplicate-free object encoded with sorted keys, compact
separators, NFC-normalized strings, no floating-point values, and one final
newline. Standard output contains only
`graphify.workspace.semantic_worker_result` version 1 frames. Standard error is
empty for a valid invocation; only invalid argv emits plain-text usage.

## Versioned request family

Every input frame belongs to the single
`graphify.workspace.semantic_worker_request` version 1 family and contains
exactly `contract`, `schema_version`, `cli_contract_version`, `action`, and the
fields defined for that action below. Unknown fields, versions, or actions fail
closed.

The first frame is `begin`, is limited to 16 KiB, and contains exactly:

- `repo_uuid`;
- `expected_registry_revision`, `expected_active_source_revision`,
  `expected_operation_epoch`, and `expected_migration_epoch`;
- `expected_queue_revision` and `expected_desired_watermark`;
- `executor`, whose only accepted value is `host_agent`;
- `host_agent_active`, which must be the Boolean `true`; and
- `timeout_ms`, an integer from 1 through 600000.

The caller therefore states live host-agent authority affirmatively. The
transport passes `host_agent_active=True` and `explicit_backend=None` to the
existing capability decision. It never consults credentials, environment
variables, provider files, `graphify.llm.detect_backend()`, or any other
provider-selection path.

After a `work` result, the process accepts at most eight optional
`checkpoint` frames followed by exactly one `complete` or `fail` frame. Every
post-claim frame carries the exact `begin_request_sha256` and `claim_id` emitted
by this process.

- `checkpoint` additionally carries one stable progress code matching
  `[a-z][a-z0-9_.:-]{0,63}` and is limited to 4 KiB. The code is not echoed in
  public output.
- `complete` additionally carries exactly one `payload`. For `UPSERT`, it is
  `{"kind":"semantic_fragment","fragment":OBJECT}`, where the fragment has
  exactly `nodes`, `edges`, and `hyperedges`. For `DELETE`, it is exactly
  `{"kind":"delete_tombstone"}`. The fragment is limited to 25 MiB; the
  complete frame is limited to 25 MiB plus 64 KiB of framing.
- `fail` additionally carries `error_code` and the actual Boolean
  `retryable`, is limited to 4 KiB, and accepts only these caller-stated pairs:

| Failure classification | Retryable |
|---|---|
| `host_agent_transient` | `true` |
| `semantic_policy_refused` | `false` |
| `semantic_work_unsupported` | `false` |

Four additional exact classifications are transport-only:
`host_agent_timeout=true`, `host_agent_interrupted=true`,
`semantic_result_invalid=true`, and `source_content_changed=false`. It derives
those only from the local deadline, EOF/interruption, bounded canonical parsing
and result validation, or exact source verification described below. They are
not accepted from a `fail` frame. The caller-accepted
`semantic_work_unsupported=false` pair is shared: the transport also derives it
when the exact work frame would exceed the public output bound.

Raw exception text, provider/model names, endpoints, credentials, arbitrary
diagnostic strings, and caller-selected paths are not request fields.

## Versioned result family

Every protocol output is one canonical, redacted, at-most-64-KiB
`graphify.workspace.semantic_worker_result` version 1 frame. Each frame has
exactly the common fields `contract`, `schema_version`,
`cli_contract_version`, and `kind`, plus the fields for one of these kinds:

| `kind` | Exact additional fields |
|---|---|
| `work` | `begin_request_sha256`, `repo_uuid`, `claim_id`, `attempt`, `work_sha256`, `work` |
| `checkpointed` | `begin_request_sha256`, `claim_id`, `checkpoint_sha256` |
| `terminal` | `outcome`, `exit_code`, and the outcome-specific fields below |

The `work` object has exactly the existing six `SemanticDesiredWork` fields:
`source_epoch`, `policy_sha256`, `operation`, `path`, `content_sha256`, and
`desired_revision`. Its path is canonical and source-relative; the frame
contains no source bytes or private absolute path. If that exact work frame
would exceed 64 KiB, the worker records `semantic_work_unsupported=false`
without exposing the oversized work.

A terminal `completed` frame additionally contains exactly
`begin_request_sha256`, `repo_uuid`, `claim_id`, `attempt`, `work_sha256`,
`payload_kind`, `payload_bytes`, `payload_sha256`, `result_binding_bytes`,
`result_binding_sha256`, `queue_revision`, and `completed_watermark`. A terminal
`idle` frame contains exactly `begin_request_sha256`, `repo_uuid`,
`queue_revision`, `desired_watermark`, and `completed_watermark`. Neither frame
contains `reason_code` or `action_code`.

`payload_kind` is exactly `semantic_fragment` for `UPSERT` or
`delete_tombstone` for `DELETE`. Each SHA-256 field is the lowercase digest of
the named canonical bytes; each byte count covers those same bytes, including
their final newline.

Every other terminal frame contains only `begin_request_sha256` when a begin
frame was accepted, plus the exact `reason_code` and `action_code` pair below.
No code accepts extension text, and no output field contains raw diagnostics:

| `outcome` / exit | Exact reason codes | Action code |
|---|---|---|
| `retry_scheduled` / 10 | `host_agent_transient`, `host_agent_timeout`, `host_agent_interrupted`, `semantic_result_invalid` | `drain_semantic_queue` |
| `withheld` / 10 | `semantic_claim_contended`, `semantic_authority_stale`, `staged_build_recovery_required` | `retry_status`, except `staged_build_recovery_required` uses `resume_exact_workspace_sync` |
| `dead_lettered` / 20 | any frozen caller or transport failure classification | `inspect_semantic_queue` |
| `invalid` / 20 | `semantic_worker_request_invalid`, `runtime_authority_missing`, `runtime_authority_invalid`, `runtime_authority_unsupported`, `unsafe_state_path`, `semantic_queue_invalid` | respectively `none`, `install_candidate_authority`, `install_candidate_authority`, `install_supported_candidate`, `configure_safe_state_root`, `inspect_semantic_queue` |
| `commit_unknown` / 20 | `semantic_worker_commit_unknown` | `none` |

The process exits 0 only after a `completed` terminal or a truthful `idle`
terminal with no eligible item. Exit 64 is reserved for invalid argv and emits
no result frame.

Downstream semantic sync may consume a staged result only when the process
exits 0 and its final frame is exactly one schema-valid `completed` terminal
whose begin-request, work, payload, and result-binding digests match the
captured session. `idle` is never semantic completion authority. A `work` or
`checkpointed` frame is never result or queue-completion authority.

## Claim and source authority

The begin request is preflighted against the stable registry, active-source,
operation, migration, queue-revision, and desired-watermark values before lease
allocation. The current working directory must be the exact Git top level of
the registry-selected active source. Existing bounded, no-follow configuration
reads and checkout verification apply before the relative work path is emitted.

After the `SEMANTIC_CLAIM` grant is accepted, the queue mutation boundary
re-reads and canonically matches the active-source workspace configuration,
derives host-agent capability from the explicit begin fields, and claims at
most one deterministic eligible item. The claim retains the existing exact
work, owner, fence, operation epoch, migration epoch, active-source revision,
attempt, and claim-ID bindings. A different queue revision or watermark,
source activation, migration, expired lease, successor attempt, or replaced
desired revision withholds the operation; none authorizes use of stale work.

Before emitting `work` and again before staging a completion, the transport
reopens the contained active-source path without following links. `UPSERT`
requires a regular file whose streamed SHA-256 exactly equals
`content_sha256`; `DELETE` requires that path to remain absent. A mismatch under
a still-current claim is the transport-owned non-retryable
`source_content_changed` failure. Activation, migration, or lease drift instead
withholds stale authority and does not rewrite it as a queue failure.

The host agent treats the verified source file as untrusted data and keeps
Graphify's extraction instructions separate from source content. The transport
does not invoke an agent, model, SDK, CLI backend, network endpoint, or
credential path. It only coordinates the already-active caller and the durable
queue.

## Result validation and binding

A `complete` frame is not queue-completion authority by itself. Under the live
claim and absolute request deadline, the implementation must perform this
ordering exactly:

1. enforce the complete-frame and payload byte limits before unbounded parse;
2. for `UPSERT`, require exactly the three fragment arrays and apply the
   existing semantic limits: at most 10,000 nodes, 100,000 edges, 10,000
   hyperedges, 256 members per hyperedge, and 256 characters per semantic ID;
3. for `UPSERT`, run `validate_semantic_fragment()`, sanitize a copy with
   `sanitize_semantic_fragment()`, validate the sanitized result again, and
   canonicalize one `semantic_fragment` payload containing only `nodes`,
   `edges`, and `hyperedges`; for `DELETE`, require and canonicalize the exact
   fieldless `delete_tombstone` payload;
4. construct one canonical internal
   `graphify.workspace.semantic_result_binding.internal` format-version-1
   envelope binding the begin-request SHA-256, repository UUID, claim ID,
   attempt, exact desired work, active-source revision, operation epoch,
   migration epoch, canonical payload byte count and SHA-256, and the sanitized
   fragment or delete tombstone;
5. atomically install that envelope at the derived private external-state path
   `workspaces/<repo_uuid>/semantic-staging/<begin_request_sha256>/result.json`
   through the existing no-follow durable-state primitives, with `0700`
   directories and a `0600` regular file;
6. reopen the installed file without following links and require exact bytes
   and SHA-256; same-byte install retry is idempotent, while different bytes at
   the same derived path are a binding conflict;
7. persist the existing bounded claim checkpoint
   `result:<result_binding_sha256>`, then reopen and rehash the envelope again;
8. under the current semantic grant, require the same claim and checkpoint,
   exact work, source revision, operation and migration epochs, and verified
   envelope; and
9. only then call the existing queue completion transition and emit the
   `completed` result.

This staging envelope does not revise
`graphify.workspace.semantic_queue.internal`, and it is not a certification
binding. A successor claim ignores an older session's staging directory. This
child does not clean orphaned staging; a separately reviewed full semantic-sync
or repair boundary must own consumption and cleanup.

The existing `bind_sealed_inputs()` transition remains later full-sync
authority. It is not called, approximated, or inferred by this worker.

## Failure, retry, and crash behavior

An accepted `fail` frame or a locally observed transport failure attempts the
existing queue failure transition exactly once under the current claim.
`failure_count` advances once. A retryable failure returns the item to `pending`
only while the explicit queue retry budget permits; otherwise, and for every
non-retryable classification, the item becomes durable `dead_letter`.
Dead-letter work continues to block reconciliation completion.

The worker maps deadline expiry to `host_agent_timeout`, EOF or interruption
before a terminal request to `host_agent_interrupted`, malformed, noncanonical,
unknown, oversized, or invalid result data to `semantic_result_invalid`, and
exact source mismatch to `source_content_changed`. It attempts one `fail()`
while the claim remains live. Unknown caller classifications, invalid Boolean
retryability, stale claims, and activation or migration drift are not rewritten
into caller-selected queue failures. They fail closed or withhold stale
authority. If the worker process itself dies before the transition, the claim
remains for the existing successor `claim_expired` recovery, which increments
the failure count once and applies the same retry-budget/dead-letter rule.

One absolute work deadline begins after the canonical begin frame is accepted
and bounds source verification, lease acquisition, frame waits, checkpoints,
payload validation and sanitization, result installation and reopen, and the
start and observed return of queue completion or caller-requested failure.
Heartbeats preserve the same owner and fence under a fixed 30-second lease TTL,
every 10 seconds while work time remains, but never extend the work deadline.

After that deadline, checkpoints and completion are forbidden and heartbeats
stop. If the claim is still live, the worker may attempt only the one
transport-owned `host_agent_timeout=true` failure and lease release before the
unchanged lease liveness deadline. If that transition cannot be proven, the
session is `commit_unknown` or the successor later applies `claim_expired`.

Commit uncertainty is phase-specific and fail-closed:

- uncertain result installation is adopted only after a no-follow reopen proves
  the exact expected bytes and digest; absence may retry under the same live
  claim, while ambiguity or different bytes is invalid;
- uncertain checkpoint persistence is adopted only when the current claim
  contains the exact result-binding checkpoint;
- once queue completion or failure begins, uncertainty is
  `semantic_worker_commit_unknown` / `none`, never success, and the same request
  is not replay authority; and
- release is cleanup, not commit acceptance. A release-only uncertain outcome
  is `commit_unknown`, and release cannot mask a primary failure.

The current completed queue item does not retain a result digest after
`complete()` clears its claim. Therefore a crash after completion begins but
before the terminal frame cannot be reconstructed as a successful public
receipt without redesigning durable queue persistence. This child reports or
leaves `commit_unknown`; operators manually inspect durable queue truth, and
downstream sync must not consume a result without the exact exit-0 `completed`
frame. A future durable completion index requires a separate contract and is
not silently invented here.

## Status and action routing

The existing read-only status schema remains authoritative and unchanged:

- pending, claimed, retrying, or watermark-gap work remains
  `semantic_queue_pending` / `drain_semantic_queue`;
- dead-letter work remains `semantic_queue_dead_letter` /
  `inspect_semantic_queue`;
- invalid queue state remains `semantic_queue_invalid` /
  `inspect_semantic_queue`;
- a nonterminal staged structural build remains
  `staged_build_recovery_required` / `resume_exact_workspace_sync` and blocks a
  new ordinary semantic claim; and
- source activation or migration drift requires fresh status and exact
  reconciliation rather than reuse of the stale claim or staging envelope.

Those actions are diagnostic routing, not mutation authority. A READY document
does not make `drain_semantic_queue` executable, and a `completed` worker result
does not make full semantic sync READY or complete.

`commit_unknown` is a direct session result with `action_code="none"`; it adds
no status reason or action. A fresh status call may truthfully report the queue
state above, but the current queue format cannot report the lost result
association. Manual inspection does not authorize staged-result consumption.

## Explicit non-goals

This first worker child excludes named or headless backends, network invocation,
direct use of `graphify.llm` provider discovery or dispatch, API-key handling,
credential inference, model selection, and automatic fallback. It also excludes
`bind_sealed_inputs()` finalization, staged-generation completion,
certification, promotion, pointer mutation, migrate, GC, repair, retained
service/watch behavior, publication, performance/resource qualification, full
semantic sync, H3, P6+, and user-global installation.

Source checkout bytes and modes, Git metadata, and real `HOME`,
`XDG_STATE_HOME`, and `CODEX_HOME` remain unchanged in verification. A future
implementation may write only the reviewed external workspace state described
above, under disposable configured roots in tests. No receipt, governance
acceptance, or implementation evidence exists for this contract-only child.
