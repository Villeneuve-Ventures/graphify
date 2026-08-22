# Host-agent semantic-worker contract

This document freezes the implemented P5B2 public host-agent semantic-worker
transport. The separately authorized delivery in PR #45 is accepted only at
this exact boundary by the
[P5B2 semantic-worker receipt](receipts/p5b2-semantic-worker.md). Full semantic
sync remains waiting, and this contract grants no successor authority.

The boundary reuses the existing `SemanticQueueStore`, `SEMANTIC_CLAIM` lease
domain, installed runtime authority, active-source policy, external durable
state root, retry/dead-letter rules, and semantic fragment sanitizer. It adds no
provider discovery, network backend, queue state-machine revision, generation
certification, promotion, or pointer authority.

## Exact public transport

The public executable is `graphify`. Its only accepted argument vector after
that executable is exactly `workspace semantic-worker --stdio`, producing this
full invocation:

```text
graphify workspace semantic-worker --stdio
```

Any other, reordered, repeated, or extended argument vector exits 64 with
deterministic usage text before installed-authority loading, standard-input
reads, source discovery, lease allocation, or state access. The valid form loads
and composes the installed `runtime-manifest.json` authority before it reads the
first protocol frame.

The command is one long-lived process for exactly one queue item. It derives
the trusted boot, PID, and process-start owner, acquires one
`SEMANTIC_CLAIM` lease, and retains that same owner and fence through claim,
optional checkpoints, the terminal request and queue transition, and release.
Any public success terminal follows that release. Separate phase-specific
subprocesses are forbidden because they cannot share the exact lease owner.

Standard input and standard output carry canonical UTF-8 JSON Lines. Each
frame is one duplicate-free object encoded with sorted keys, compact
separators, NFC-normalized strings, and one final newline. Binary
floating-point values are forbidden. The only non-integer JSON number tokens
are `confidence_score` and `weight` inside an `UPSERT` semantic fragment. Each
is an exact unsigned decimal value from 0 through 1 with at most six fractional
digits. Canonical tokens use no exponent, leading plus, unnecessary integer-part
zero, or insignificant trailing zero; a fraction below 1 retains its single
required `0` before the decimal point. The endpoints are `0` and `1`, not `-0`,
`0.0`, or `1.0`. Parsers retain those tokens as exact decimal values rather than
first rounding them through binary floating point. Standard output contains only
`graphify.workspace.semantic_worker_result` version 1 frames. Standard error is
empty for a valid invocation; only an invalid argument vector emits plain-text
usage.

The implementation extends `validate_semantic_fragment()` with a lossless
canonical-number encoder hook while preserving its default for all other
callers. Both worker calls to that helper use the hook to encode
the retained exact-decimal values as their original canonical, unquoted JSON
number tokens. Coercion to binary float or JSON string is forbidden.
`sanitize_semantic_fragment()` receives a copy that retains the exact-decimal
values, and every surviving score or weight must remain exactly equal after
sanitization.

## Versioned request family

Every input frame belongs to the single
`graphify.workspace.semantic_worker_request` version 1 family and contains
exactly the four common fields `contract`, `schema_version`,
`cli_contract_version`, and `action`, plus the action-specific fields defined
below. Unknown fields, versions, or actions fail closed.

The first frame is `begin`, is limited to 16 KiB, and has exactly these
additional action-specific fields:

- `repo_uuid`;
- `expected_registry_revision`, `expected_active_source_revision`,
  `expected_operation_epoch`, and `expected_migration_epoch`;
- `expected_queue_revision` and `expected_desired_watermark`;
- `executor`, whose only accepted value is `host_agent`;
- `host_agent_active`, which must be the Boolean `true`; and
- `timeout_ms`, an integer from 1 through 600000.

`repo_uuid` is a JSON string in canonical lowercase hyphenated RFC-variant UUID
form, version 1 through 8. The three expected registry, active-source, and
operation values are JSON integers from 1 through 9223372036854775807. Expected
migration epoch, queue revision, and desired watermark are JSON integers from 0
through 9223372036854775807. Of these six authority coordinates, zero is valid
only for the latter three; zero queue revision or desired watermark denotes
initial queue state. Booleans are not integers. A type, range, or UUID violation
makes the frame unaccepted and follows `semantic_worker_request_invalid`, not
`semantic_authority_stale`.

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
  `[a-z][a-z0-9_.:-]{0,63}`, must not start with the reserved `result:` prefix,
  and is limited to 4 KiB. The code is not echoed in public output.
- `complete` additionally carries exactly one `payload`. For `UPSERT`, it is
  `{"kind":"semantic_fragment","fragment":OBJECT}`, where the fragment has
  exactly `nodes`, `edges`, and `hyperedges` and the closed nested schema below.
  For `DELETE`, it is exactly `{"kind":"delete_tombstone"}` with no additional
  field. The raw fragment and canonical sanitized fragment are each limited to
  25 MiB; the complete frame is limited to 25 MiB plus 64 KiB of framing.
- `fail` additionally carries `error_code` and the actual Boolean
  `retryable`, is limited to 4 KiB, and accepts only these caller-stated pairs:

| Failure classification | Retryable |
|---|---|
| `host_agent_transient` | `true` |
| `semantic_policy_refused` | `false` |
| `semantic_work_unsupported` | `false` |

Six additional exact classifications are transport-only after a live queue
claim exists:
`host_agent_timeout=true`, `host_agent_interrupted=true`,
`semantic_result_invalid=true`, `source_unavailable=true`,
`source_content_changed=false`, and `semantic_result_binding_conflict=false`.
The transport derives those only from the local deadline, EOF or catchable
interruption after claim, bounded canonical parsing and result validation, an
incomplete source observation, a completed source observation proving mismatch,
or an exact different-byte result-staging conflict described below. They are not
accepted from a `fail` frame. The caller-accepted
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

For a `terminal` frame with `outcome="completed"`, the exact outcome-specific
fields are `begin_request_sha256`, `repo_uuid`, `claim_id`, `attempt`,
`work_sha256`,
`payload_kind`, `payload_bytes`, `payload_sha256`, `result_binding_bytes`,
`result_binding_sha256`, `queue_revision`, and `completed_watermark`. For
`outcome="idle"`, they are exactly `begin_request_sha256`, `repo_uuid`,
`queue_revision`, `desired_watermark`, and `completed_watermark`. Neither
outcome has `reason_code` or `action_code`.

Result scalars are closed as follows:

| Fields | Exact JSON type and rule |
|---|---|
| `contract`; `schema_version`; `cli_contract_version` | The result-family string above; integer `1`; integer `1` |
| `kind`; `outcome`; `payload_kind`; `reason_code`; `action_code` | Strings limited to the frozen values in this section |
| `exit_code` | Integer `0` for `completed` or `idle`, `10` for `retry_scheduled` or `withheld`, and `20` for `dead_lettered`, `invalid`, or `commit_unknown` |
| `repo_uuid`; `claim_id`; every `*_sha256` | The canonical UUID string above; exactly 64 lowercase hexadecimal digits for the claim ID and each digest |
| `attempt`; `source_epoch`; `desired_revision`; `payload_bytes`; `result_binding_bytes` | Integers greater than zero |
| `queue_revision`; `desired_watermark`; `completed_watermark` | Integers greater than or equal to zero |
| Nested work `operation`; `path`; policy/content digests | `UPSERT` or `DELETE`; the canonical source-relative string above; the same digest rule. Every value equals the live claim |

Integers are arbitrary precision; Booleans, quoted integers, and fractional
tokens are invalid. The byte counts equal their recomputed preimage lengths and
obey the applicable limits. Every queue value equals the observed durable
snapshot, and `idle` additionally requires
`completed_watermark <= desired_watermark`. Any type, range, equality, or enum
violation is not a schema-valid result.

`payload_kind` is exactly `semantic_fragment` for `UPSERT` or
`delete_tombstone` for `DELETE`. `payload_bytes` and `payload_sha256` cover the
exact canonical bytes, including the final newline, of the whole validated
operation-matched `complete.payload` object: the
`{"kind":"semantic_fragment","fragment":...}` wrapper for `UPSERT`, or the
kind-only `{"kind":"delete_tombstone"}` object for `DELETE`. Hashing only the
nested fragment, a tombstone surrogate, or the enclosing result-binding
envelope is not equivalent. The result-binding envelope stores that exact
payload object once. `result_binding_bytes` and `result_binding_sha256` cover
the whole canonical envelope bytes, also including their final newline.

The request/work/checkpoint digest fields are lowercase hexadecimal over one
exact preimage:

- `begin_request_sha256` hashes the entire accepted canonical `begin` request
  frame, including its final newline;
- `work_sha256` hashes the exact canonical six-field `work` object, including
  its final newline, not the enclosing `work` result frame; and
- `checkpoint_sha256` hashes the entire accepted canonical `checkpoint` request
  frame, including its final newline. It therefore binds the begin-request
  digest, claim ID, and private progress code without echoing that code.

Hashing the action-specific value alone, any enclosing result frame, or bytes
without the required final newline is not equivalent.

For every other terminal outcome, the exact outcome-specific fields are
`reason_code` and `action_code`, plus `begin_request_sha256` if and only if a
begin frame was accepted. No code accepts extension text, and no output field
contains raw diagnostics:

| `outcome` / exit | Exact reason codes | Action code |
|---|---|---|
| `retry_scheduled` / 10 | `host_agent_transient`, `host_agent_timeout`, `host_agent_interrupted`, `semantic_result_invalid`, `source_unavailable` | `drain_semantic_queue`, except `source_unavailable` uses `restore_source` |
| `withheld` / 10 | `semantic_claim_contended`, `semantic_authority_stale`, `semantic_worker_preclaim_timeout`, `semantic_worker_preclaim_interrupted`, `semantic_checkpoint_capacity_unavailable`, `workspace_config_unavailable`, `semantic_capability_unavailable`, `staged_build_recovery_required` | `retry_status`, except `semantic_checkpoint_capacity_unavailable` uses `inspect_semantic_queue`, `semantic_capability_unavailable` uses `inspect_workspace_state`, and `staged_build_recovery_required` uses `resume_exact_workspace_sync` |
| `dead_lettered` / 20 | any frozen caller or transport failure classification | `inspect_semantic_queue` |
| `invalid` / 20 | `semantic_worker_request_invalid`, `runtime_authority_missing`, `runtime_authority_invalid`, `runtime_authority_unsupported`, `unsafe_state_path`, `workspace_config_invalid`, `semantic_queue_invalid`, `registry_invalid`, `workspace_state_invalid` | respectively `none`, `install_candidate_authority`, `install_candidate_authority`, `install_supported_candidate`, `configure_safe_state_root`, `inspect_workspace_state`, `inspect_semantic_queue`, `inspect_workspace_state`, `inspect_workspace_state` |
| `commit_unknown` / 20 | `semantic_worker_commit_unknown` | `none` |

Before an accepted `begin`, observed EOF, a catchable interruption, or malformed,
noncanonical, unknown, or oversized input emits `invalid` /
`semantic_worker_request_invalid` / `none`, omits `begin_request_sha256`, and
performs no source discovery, lease allocation, or queue mutation.

Before a current-session queue claim exists, the worker may not call the queue
failure transition or attribute a `failure_count` increment to that session.
This does not suppress the existing deterministic `claim_expired` recovery
inside `claim()`: predecessor attempts may advance failure state once and retry
or dead-letter under the frozen queue budget before `claim()` installs or
returns a current-session claim. Deadline expiry in source/config preflight or
lease acquisition is `semantic_worker_preclaim_timeout`; a catchable
interruption after `begin` but before a current-session claim is
`semantic_worker_preclaim_interrupted`. Missing or transiently unreadable active
configuration is `workspace_config_unavailable`; malformed configuration is
`workspace_config_invalid`; a policy that does not authorize the explicitly
active host agent is `semantic_capability_unavailable`.
Registry, source, checkout, configuration-digest, operation, migration, queue,
or watermark CAS drift is `semantic_authority_stale`. Lease contention and the
staged-build barrier retain their named outcomes above. These mappings apply
only when rejection is proved to precede current-session queue-claim mutation,
whether before acquisition or after a grant. Before `claim()`, the worker retains
the exact queue snapshot and the deterministic projected candidate. If the call
is uncertain, one locked reread adopts an exact installed current-session claim;
an exact unchanged snapshot may retry while the work deadline remains or emit
truthful `idle` when it proves no eligible item. A snapshot containing only the
exact projected predecessor `claim_expired` recovery is adopted only when it
also proves that no item is eligible, and then emits truthful `idle`. Any other
absent-claim state, unreadable state, or ambiguity is
`semantic_worker_commit_unknown`; absence alone is not a preclaim-failure route.

Registry corruption proved before any potentially mutating store call is
`registry_invalid`; lease-state corruption proved at that boundary is
`workspace_state_invalid`. Both are `invalid` / `inspect_workspace_state`, do
not call the current-session queue failure transition, and do not claim release.
If acquisition, heartbeat, or release may have mutated durable state, unreadable
or ambiguous state follows the phase-specific
`semantic_worker_commit_unknown` rules instead.

The process exits 0 only after a `completed` terminal or a truthful `idle`
terminal with no eligible item. Exit 64 is reserved for an invalid argument
vector and emits no result frame.

An `idle` path that acquired a semantic lease also proves release of that exact
owner/fence before emitting its terminal. Release failure or uncertainty without
a primary failure follows `semantic_worker_commit_unknown`, using the same
locked reread rule as completion.

Downstream semantic sync may consume a staged result only when the process
exits 0 and its final frame is exactly one schema-valid `completed` terminal
whose begin-request, work, payload, and result-binding digests match the
captured session. `idle` is never semantic completion authority. A `work` or
`checkpointed` frame is never result or queue-completion authority.

Each result frame is fully encoded before a deadline-aware write-all and flush.
The delivery deadline is five seconds after encoding; `work` and `checkpointed`
use the earlier absolute work deadline. A terminal delivery window, whether
before `begin` or after work-deadline cleanup, grants no queue or lease authority
and extends neither deadline. The writer uses nonblocking readiness waits or an
equivalent primitive; short-write retries and any buffering-layer flush share
the delivery deadline. Emission succeeds only after every byte, including the
final newline, is written and flushed. Deadline exhaustion, closed output, zero
progress, partial-then-error, broken pipe, or flush failure is an output-delivery
failure. A receiver accepts only a complete canonical newline-terminated frame.
Partial trailing bytes are not a frame, and complete-looking bytes are not
completion authority without the required exit 0.

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

The locked claim-admission candidate must preserve capacity for the mandatory
result checkpoint without revising the durable queue schema. After applying any
deterministic predecessor recovery and projecting the new claim, the worker
replaces that claim's checkpoint in a copy with `result:` followed by 64
lowercase hexadecimal digits and measures the exact canonical queue bytes. It
installs the claim only when that projected snapshot is within `max_bytes`.
Every later queue mutation while the claim remains live applies the same
projection, so enqueue, reconciliation, or another checkpoint cannot consume
the reserved headroom. Failure to preserve that headroom before claim is
`semantic_checkpoint_capacity_unavailable` / `inspect_semantic_queue`; it
installs no current-session claim and does not suppress any already-proved
predecessor `claim_expired` recovery.

Before emitting `work`, again before staging a completion, and finally after all
envelope and authority checks immediately before `complete()`, the transport
reopens the contained active-source path without following links. `UPSERT`
requires a regular file whose streamed SHA-256 exactly equals
`content_sha256`; `DELETE` requires that path to remain absent. A mismatch under
a still-current claim is the transport-owned non-retryable
`source_content_changed` failure only after a completed observation proves the
different content or presence state. An open, stat, permission, or read error,
including a short-read fault that leaves the observation unable to prove the
expected bytes or absence, is the transport-owned retryable
`source_unavailable` failure.
Activation, migration, or lease drift instead withholds stale authority and does
not rewrite it as either queue failure.

The host agent treats the verified source file as untrusted data and keeps
Graphify's extraction instructions separate from source content. The transport
does not invoke an agent, model, SDK, CLI backend, network endpoint, or
credential path. It only coordinates the already-active caller and the durable
queue.

## Result validation and binding

A `complete` frame is not queue-completion authority by itself. Under the live
claim and absolute request deadline, a worker-specific closed validator must
surround the existing general semantic helpers. The untrusted pre-sanitize
fragment entries have exactly these required fields; no field is optional and
no alias or unknown nested key is accepted:

| Entry | Exact fields |
|---|---|
| node | `id`, `label`, `file_type`, `source_file`, `source_location`, `source_url`, `captured_at`, `author`, `contributor` |
| edge | `source`, `target`, `relation`, `confidence`, `confidence_score`, `source_file`, `source_location`, `weight` |
| hyperedge | `id`, `label`, `nodes`, `relation`, `confidence`, `confidence_score`, `source_file` |

Node and hyperedge IDs and every edge endpoint/member use the existing semantic
ID grammar and length bound. Node IDs are unique, hyperedge IDs are unique, all
edge endpoints name input nodes, and every hyperedge has 2--256 pairwise-distinct
members that name input nodes.
`label` is a nonempty, trimmed string of at most 16 KiB in UTF-8 and contains no
Unicode general-category `Cc` code point. `file_type` is one of `code`,
`document`, `paper`, `image`, `rationale`, or `concept`. Edge `relation` is one
of `calls`, `implements`,
`references`, `cites`, `conceptually_related_to`, `shares_data_with`,
`semantically_similar_to`, or `rationale_for`; edge `confidence` is one of
`EXTRACTED`, `INFERRED`, or `AMBIGUOUS`. Hyperedge `relation` is one of
`participate_in`, `implement`, or `form`; hyperedge `confidence` is
`EXTRACTED` or `INFERRED`. Every score and weight is present and follows the
exact fixed-point rule above. Every `source_file` is exactly the claimed
source-relative `work.path`; `source_location` is null or `L` followed by a
positive base-10 line number, with a 32-byte UTF-8 maximum. `source_url`,
`captured_at`, `author`, and `contributor` are null. Thus designated labels are
the only caller-supplied source-derived prose admitted to the pre-sanitize
fragment; each is bounded and
there is no separate field for a private path, raw-source blob, provider data,
credential metadata, or arbitrary extension. This syntactic boundary does not
claim that admitted semantic prose is non-verbatim, which is why no payload text
is copied into a public result.

Sanitization may remove nodes and add only one optional `rationale` field to a
surviving node. That field consists solely of admitted candidate labels joined
by the sanitizer's exact double-newline separator and is at most 16 KiB in
UTF-8. The post-sanitize schema otherwise retains the exact field sets and
value rules above, permits only `code`, `document`, `paper`, or `image` as
surviving file types, and requires every retained edge and hyperedge member to
name a surviving node. Every retained hyperedge has at least two
pairwise-distinct members. Duplicate input members are rejected before the
sanitizer, not deduplicated by it.

The implementation must perform this ordering exactly:

1. enforce the complete-frame and payload byte limits before unbounded parse;
2. for `UPSERT`, enforce the closed pre-sanitize schema plus the existing
   semantic limits: at most 10,000 nodes, 100,000 edges, 10,000 hyperedges,
   2--256 pairwise-distinct members per hyperedge, and 256 characters per
   semantic ID. Reject duplicate members before sanitizer invocation, then run
   `validate_semantic_fragment()` through the exact-decimal encoder hook above;
3. build one `rationale_for` source-to-target index in a single edge pass,
   identify rationale candidates in one node pass, and precompute expansion
   before concatenation. Reject before sanitizer invocation if any projected
   `rationale` exceeds 16 KiB or the projected canonical sanitized fragment
   exceeds 25 MiB. Rationale propagation must be
   `O(nodes + edges + rationale_fanout)`; a candidate-by-all-edges scan is not a
   conforming implementation;
4. sanitize an exact-decimal-preserving copy with
   `sanitize_semantic_fragment()` using that bounded indexed path, run
   `validate_semantic_fragment()` again through the same encoder hook, enforce
   the closed post-sanitize schema and exact surviving numeric values, and
   measure the actual canonical sanitized fragment against the same per-string
   and 25-MiB limits;
5. for `UPSERT`, construct and canonicalize the exact whole payload object
   `{"kind":"semantic_fragment","fragment":SANITIZED_FRAGMENT}`; for `DELETE`,
   require and canonicalize the exact kind-only
   `{"kind":"delete_tombstone"}` object;
6. construct one canonical internal result-binding envelope using exactly the
   object grammar below. Uppercase names are typed metavariables; `WORK` is the
   exact six-field object above and `PAYLOAD` the exact step-5 operation-matched
   object. No other field or nesting is permitted:

   ```text
   {
     "active_source_revision": ACTIVE_SOURCE_REVISION,
     "attempt": ATTEMPT,
     "begin_request_sha256": "BEGIN_REQUEST_SHA256",
     "claim_id": "CLAIM_ID",
     "contract": "graphify.workspace.semantic_result_binding.internal",
     "format_version": 1,
     "migration_epoch": MIGRATION_EPOCH,
     "operation_epoch": OPERATION_EPOCH,
     "payload": PAYLOAD,
     "payload_bytes": PAYLOAD_BYTES,
     "payload_sha256": "PAYLOAD_SHA256",
     "repo_uuid": "REPO_UUID",
     "work": WORK,
     "work_sha256": "WORK_SHA256"
   }
   ```

   The integer and string values equal the accepted request and live claim;
   `work_sha256` equals the work digest defined above, while `payload_bytes` and
   `payload_sha256` equal the byte count and digest of the exact payload preimage.
   The envelope is encoded by the same canonical JSON rule with one final
   newline. `result_binding_bytes` is the length of those whole bytes and
   `result_binding_sha256` is their SHA-256;
7. atomically install that envelope at the derived private external-state path
   `workspaces/<repo_uuid>/semantic-staging/<begin_request_sha256>/result.json`
   through the existing no-follow durable-state primitives, with `0700`
   directories and a `0600` regular file;
8. reopen the installed file without following links and require exact bytes
   and SHA-256; same-byte install retry is idempotent. Exact different bytes at
   the same derived path, while the claim remains provably current, are the
   transport-owned non-retryable `semantic_result_binding_conflict=false`
   failure. One `fail()` transition makes the item `dead_lettered`, with
   `inspect_semantic_queue` as the public action;
9. persist the existing bounded claim checkpoint
   `result:<result_binding_sha256>`, then reopen and rehash the envelope again
   and require that reopened SHA-256 to equal the checkpoint suffix;
10. parse and revalidate that exact envelope. Require its
    `begin_request_sha256` to equal the captured digest of the accepted canonical
    begin frame and its `repo_uuid` to equal that request field; require its
    `claim_id`, `attempt`, exact desired work, and `work_sha256` to equal the live
    claim and captured work result. Require its payload object, byte count, and
    digest to equal the validated payload. Under the current semantic grant,
    revalidate the same source revision, owner, fence, operation epoch, and
    migration epoch;
11. perform the final no-follow source-content check defined above. Any exact
    content or absence mismatch follows `source_content_changed` while the claim
    remains current;
12. only then call the existing queue completion transition and require its
    observed successful return;
13. release the exact semantic lease and prove that owner/fence is no longer
    installed, using the release return or the locked reread rule below; and
14. only then emit the `completed` result.

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

With a live claim, the transport maps deadline expiry to `host_agent_timeout`;
EOF while awaiting a request or catchable interruption before terminal
acceptance to `host_agent_interrupted`; invalid result data to
`semantic_result_invalid`; incomplete source observation to
`source_unavailable`; proven source mismatch to `source_content_changed`; and an
exact different-byte staging conflict to `semantic_result_binding_conflict`.
After an accepted `complete`, the interruption route remains available through
validation, sanitization, hashing, installation, and other work until queue
completion or failure mutation begins. After an accepted `fail`, its caller
classification remains primary until that failure transition starts. Each
transport-owned failure attempts one `fail()` while the claim remains live.
Unknown caller classifications, invalid Boolean retryability, stale claims, and
activation or migration drift are not rewritten into caller-selected queue
failures. They fail closed or withhold stale authority. If the worker process
itself dies before the transition, the claim remains for the existing successor
`claim_expired` recovery, which increments the failure count once and applies
the same retry-budget/dead-letter rule.

One absolute work deadline begins after the canonical begin frame is accepted
and bounds source verification, lease acquisition, frame waits, checkpoints,
payload validation and sanitization, result installation and reopen, every
`work` or `checkpointed` readiness/write/flush wait, and the start and observed
return of queue completion or caller-requested failure.
Heartbeats preserve the same owner and fence under a fixed 30-second lease TTL,
every 10 seconds while work time remains, but never extend the work deadline.

If the deadline expires before current-session queue-claim mutation begins, the
worker emits the preclaim timeout outcome above after any provable lease
release. An uncertain `claim()` call that installed no current-session claim
instead follows the exact retry, `idle`, or commit-unknown reread rules above;
absence does not imply timeout. The worker never synthesizes
`host_agent_timeout`, calls `fail()`, or advances a queue failure count for the
current session without a live claim. Deterministic predecessor `claim_expired`
recovery remains permitted as specified above. The same current-session
no-claim rule applies to interruption, checkout, configuration, capability,
CAS, contention, capacity, and staged-barrier rejection.

Output-delivery failure never triggers another write. The worker exits 20 and
applies these phase rules:

- before a current-session claim, leave the queue unchanged after any required
  lease cleanup;
- while emitting `work` or `checkpointed` before queue completion or failure
  mutation, work-deadline expiry takes the `host_agent_timeout=true` route.
  Delivery-deadline expiry while work time remains, or any other delivery
  failure, takes `host_agent_interrupted=true` once while the claim is live,
  then applies normal failure cleanup; and
- while emitting a terminal, do not repeat the determined queue transition or
  lease release. A lost `idle` leaves the queue unchanged; a lost `completed` is
  publicly `commit_unknown` because the queue cannot reconstruct its result
  association.

Downstream consumption remains forbidden even if all frame bytes reached the
receiver before the writer observed failure.

After that deadline, checkpoints and completion are forbidden and heartbeats
stop. If the claim is still live, the worker may attempt only the one
transport-owned `host_agent_timeout=true` failure and lease release before the
unchanged lease liveness deadline. If that transition cannot be proven, the
session is `commit_unknown` or the successor later applies `claim_expired`.

Commit uncertainty is phase-specific and fail-closed:

- before lease acquisition, retain the exact lease-state revision, fence high
  watermark, operation and migration epochs, and absence of a live
  `SEMANTIC_CLAIM`. After an uncertain acquisition, one locked reread may adopt
  only the unique next live lease for the derived owner with the expected source
  authority and exact one-step fence/domain-epoch advances. Proven absence may
  reacquire while the work deadline remains; a live different owner is
  `semantic_claim_contended`, authority drift is `semantic_authority_stale`, and
  an unreadable or non-unique state is `semantic_worker_commit_unknown`;
- after an uncertain heartbeat, one locked reread adopts only the same
  owner/fence/epochs with the exact requested heartbeat timestamp and liveness
  deadline. The exact unchanged pre-heartbeat record may retry while both
  deadlines remain. Absence, replacement, or expiry withholds stale authority;
  any other or unreadable state is `semantic_worker_commit_unknown`. No
  checkpoint, completion, or failure may proceed from the pre-heartbeat grant
  until that reread proves one of those states. A deterministic registry or
  lease-state corruption proved before heartbeat mutation instead follows the
  named `registry_invalid` or `workspace_state_invalid` route above;
- uncertain result installation is adopted only after a no-follow reopen proves
  the exact expected bytes and digest; absence may retry under the same live
  claim. Exact different bytes follow the binding-conflict failure route only
  while the claim is provably current; unreadable or ambiguous state is
  `semantic_worker_commit_unknown`;
- before any optional or mandatory checkpoint call, retain the exact current
  claim and its prior checkpoint. After uncertain persistence, one locked reread
  adopts only the same live claim containing the exact requested checkpoint; the
  exact retained pre-call claim may retry while the work and lease deadlines
  remain. Any other checkpoint, absent or stale claim, or unreadable state is
  `semantic_worker_commit_unknown` and cannot proceed. An adopted optional
  checkpoint emits `checkpointed` with the digest of its exact request frame; an
  adopted mandatory `result:<result_binding_sha256>` checkpoint may continue
  only to the envelope reopen and completion checks above;
- once queue completion or failure begins, uncertainty is
  `semantic_worker_commit_unknown` / `none`, never success, and the same request
  is not replay authority; and
- before release, retain the exact semantic lease record. On the success path,
  release follows a proven queue completion but precedes the `completed` frame.
  An `idle` success path applies the same proof before its terminal.
  An observed release return or one locked reread proving that exact owner/fence
  absent permits the frame; the exact unchanged record may retry only before
  its liveness deadline. Deterministic registry or lease-state corruption proved
  before release mutation follows its named invalid route and permits no success
  frame. When release may have mutated, unreadable or ambiguous state is
  `commit_unknown`, so no success frame has yet been emitted. Release is cleanup,
  not commit acceptance: any other release-only failure or uncertainty after
  proven completion is `commit_unknown`, and release cannot mask a primary
  failure.

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

This child also supplies no content-level DLP or secret classifier for the
bounded semantic `label` and `rationale` fields. Private staging remains
untrusted `0600` state and is never public output. Any policy that rejects or
redacts designated semantic prose by content, rather than by the closed schema
above, requires a separately reviewed contract and implementation packet.
The [frozen release/DLP decision](#p5b2-semantic-content-releasedlp-decision)
preserves this worker boundary: it remains `WAITING` and grants no implementation
or content-release effect.

Source checkout bytes and modes, Git metadata, and real `HOME`,
`XDG_STATE_HOME`, and `CODEX_HOME` remain unchanged in verification. The
accepted implementation writes only the reviewed external workspace state
described above, under disposable configured roots in tests. Its bounded
implementation and governance evidence is recorded in the
[P5B2 semantic-worker receipt](receipts/p5b2-semantic-worker.md).

## P5B2 semantic-result handoff and sealed-input finalization

This is a separate, implemented, and accepted unnumbered child. It does not
revise the accepted worker transport or its receipt. Its exact delivery and
governance evidence is bound by the
[P5B2 semantic-result handoff receipt](receipts/p5b2-semantic-result-handoff.md).
Merged PR #53 made that governance acceptance canonical on `workspace/v1`; it
promotes no successor.

The child has no public command, request family, result family, status field,
or runtime receipt. It is an internal composition of the accepted worker evidence, the
existing semantic queue, the request-bound staged-build lifecycle, generation
staging, and `bind_sealed_inputs()`. It begins only after all semantic work for
one exact reconciliation is independently consumable and stops immediately
after the resulting staged-payload manifest is durably bound to that
reconciliation.

### Entry authority and exact result set

For every desired work identity in the current semantic-required
reconciliation, the handoff must receive either:

1. one freshly captured worker session whose process exited exactly 0, whose
   final and only terminal frame is one schema-valid `completed` result, and
   whose begin, work, payload, and result-binding digests match the captured
   session and reopened immutable `result.json`; or
2. the exact same worker-session evidence and result-binding envelope copied
   from `graphify-out/semantic-inputs.json` in the exact current certified
   source generation selected by the structural request's pointer/receipt CAS,
   where that file is a verified format-version-1 handoff containing the
   identical `SemanticDesiredWork` identity.

The second form is only carried completion, not inference. Its original
canonical begin request, complete stdout transcript, observed process exit,
and result-binding envelope remain embedded and revalidated. A prior queue
status, completed watermark, generation receipt, or payload manifest without
that exact handoff entry is not a substitute.

The target generation is separate authority. It is exactly the existing
canonical `SyncRequest.generation_id` whose complete request digest equals the
`StructuralBuildRequest.logical_request_sha256`; this child adds no public
request field. The target must satisfy the existing generation grammar and must
not name the current certified source generation. Before first handoff
installation it must not name any existing staging or certified generation.
After the exact handoff exists, target staging may exist only when the existing
staged-build record binds the same repository, target, and structural request in
the exact `REQUESTED`, `PUBLISHING`, or `COMPLETE` recovery lifecycle. A
certified target, unbound staging, or any mismatched request remains a conflict.
When carried completion is used, the current source generation is recorded
independently and may never be substituted for, or swapped with, the target.

The consumer first parses all inputs as untrusted bytes and reopens every
referenced worker result without following links. It then takes the registry
lock followed by the workspace lock and revalidates all of the following as one
snapshot:

- the repository UUID, exact target generation, complete `SyncRequest` digest,
  registry revision, active-source revision, operation and migration epochs,
  current pointer CAS, optional carried-source generation, selected
  compatibility, and full `StructuralBuildRequest`;
- the source commit, source epoch, policy hash, detector identity, observation
  manifest, observation-entry digest, and two-equal-observation evidence;
- the queue revision and canonical-state hash, queue policy, compaction epoch,
  desired and completed watermarks, exact semantic-required reconciliation,
  desired-set hash, and still-null sealed-input binding; and
- a bijection between the reconciliation's desired work and the supplied
  result entries.

The completed watermark must equal the desired watermark. Every retained queue
item must be completed; a compacted queue is allowed because the reconciliation
retains the exact desired set. Each desired identity has exactly one result and
each result names exactly one desired identity. Missing, duplicate, stale,
foreign, conflicting, or extra results fail before a staged-build request,
generation directory, queue binding, or cleanup is created. `idle`, `work`,
`checkpointed`, a partial or non-final terminal, a nonzero process exit,
`commit_unknown`, manual inspection, an orphan `result.json`, or a cleared
`result:` checkpoint never satisfies this entry gate.

### Durable handoff record

The internal handoff owner is the trusted workspace lifecycle composition. The
accepted worker remains only the producer of result envelopes and public
session evidence; it does not install this successor record. The semantic queue
owns reconciliation truth and the final sealed-input binding, while
`GenerationStore` continues to own request-bound staging and payload-manifest
completion.

One immutable record is installed at this derived private external-state path:

```text
workspaces/<repo_uuid>/semantic-staging/handoffs/
  <target_generation_id>/<structural_request_sha256>.json
```

The directory chain is descriptor-relative, contained, no-follow `0700`; the
record is one regular, single-link `0600` file. The target generation ID must
satisfy the existing generation grammar and the final name is the lowercase
SHA-256 of the complete canonical `StructuralBuildRequest`. Caller-selected
absolute paths, aliases, links, special files, and alternate names are
forbidden.

The record contract is
`graphify.workspace.semantic_result_handoff.internal`, format version 1. It is
canonical UTF-8 JSON with NFC-normalized strings, lexicographically sorted
object keys, compact separators, exact unquoted worker fixed-point decimals,
and one final newline. Binary floating point is forbidden. The top-level field
set is exactly:

```text
{
  "contract": "graphify.workspace.semantic_result_handoff.internal",
  "format_version": 1,
  "repo_uuid": REPO_UUID,
  "target_generation_id": TARGET_GENERATION_ID,
  "carried_source_generation_id": CARRIED_SOURCE_GENERATION_ID_OR_NULL,
  "structural_request": STRUCTURAL_BUILD_REQUEST,
  "structural_request_sha256": STRUCTURAL_REQUEST_SHA256,
  "queue": QUEUE_BINDING,
  "results": [RESULT_ENTRY, ...],
  "materialized": [MATERIALIZED_ENTRY, ...]
}
```

Format version 1 is a closed compatibility boundary. The structural request's
`compatibility_sha256` must equal the currently selected existing
`GenerationStore` compatibility digest, and every embedded worker object must
remain valid under the accepted worker version-1 grammar. An unknown field,
contract name, format version, encoder rule, worker grammar, or compatibility
digest is unsupported rather than forward-compatible. This child performs no
in-place rewrite or migration; any successor representation requires a
separately frozen version and may not replace bytes at this derived path.

`target_generation_id` is the exact new generation from the validated existing
`SyncRequest`; the same value is passed unchanged to `request_staged_build()`,
request-bound acquisition, allocation, staging, completion, and replay.
`carried_source_generation_id` is exactly `null` when every result is fresh.
Otherwise it is the canonical generation ID selected by the captured current
pointer, its receipt digest equals
`structural_request.expected_current_receipt_sha256`, and it is distinct from
the target. Every carried entry comes from this one source generation. A null
current receipt, missing source generation, source/target equality or swap,
different target on replay, or disagreement among the pointer, receipt,
payload manifest, semantic-input bytes, and recorded source identity is a
conflict that blocks first installation or exact replay.

`QUEUE_BINDING` has exactly `active_source_revision`, `revision`,
`canonical_state_sha256`, `completed_watermark`, `desired_watermark`,
`compaction_epoch`, `queue_policy`, and `reconciliation`. The queue policy is
the complete existing version-1 `SemanticQueuePolicy`. The reconciliation is
the complete current internal reconciliation, including source observations,
the semantic-required bit, ordered desired work, desired-set digest, and a null
`sealed_input_manifest_sha256`. The structural request is the complete existing
request object; its digest is recomputed from its canonical bytes. Together
these objects bind the repository, active source, registry, operation and
migration epochs, pointer CAS, source/policy evidence, capacity and
compatibility hashes, distinct target and optional carried-source generations,
queue/watermark state, and exact desired set.

Each `RESULT_ENTRY` has exactly `origin`, `begin_request`,
`begin_request_sha256`, `session`, `result_binding`, `result_binding_bytes`, and
`result_binding_sha256`:

- `origin` is exactly `fresh_worker_session` or
  `carried_current_generation`. The latter requires the non-null recorded
  carried-source generation; the former may not claim that provenance. This is
  hop-local wrapper metadata, not part of the immutable accepted worker evidence;
- `begin_request` is the exact accepted canonical version-1 begin object and
  its digest covers the complete canonical frame including the final newline;
- `session` has exactly `frames`, `stdout_bytes`, `stdout_sha256`, and
  `process_exit_code`. `frames` is the complete ordered list of canonical
  worker-result objects; their canonical newline-terminated concatenation must
  reproduce the byte count and SHA-256. It begins with one matching `work`
  frame, may contain at most eight matching `checkpointed` frames, and ends
  with the one and only terminal, which is `completed`. The process exit is the
  integer 0;
- `result_binding` is the complete reopened canonical
  `graphify.workspace.semantic_result_binding.internal` version-1 envelope,
  including its exact payload once; and
- the result-binding byte count and digest cover that entire canonical envelope
  including its final newline.

The begin, work, claim, attempt, repository, active-source revision, worker
operation and migration epochs, payload kind, payload bytes and digest, and
result bytes and digest must agree wherever those fields overlap in the begin
request, session frames, and envelope. Every entry's repository UUID equals the
outer record. Its active-source revision and migration epoch equal the captured
queue and structural-request authority, and its desired work retains the exact
current source epoch and policy hash. Original registry, worker-operation,
queue-revision, and watermark coordinates are preserved, not rewritten as the
later staging coordinates. For a fresh entry, the begin request's active-source,
migration, and desired-watermark expectations must equal the captured current
values; its original global registry coordinate is retained while the same
repository entry is independently revalidated at the current registry revision.
A carried entry may retain older registry, operation, queue, and watermark
coordinates only inside its exact original evidence; its repository, active
source, migration epoch, and desired identity must still match the current
handoff. At least one `carried_current_generation` entry requires the non-null
carried-source field, and that field must be null when no entry has that origin.

Each completed terminal's queue revision and completed watermark remain its
original observed post-completion values; they must not exceed the captured
current queue revision or completed watermark. The locked current reconciliation
independently proves that every exact work identity is complete. A carried entry
sets its new wrapper `origin` to `carried_current_generation` while reproducing
the source entry's `begin_request`, `begin_request_sha256`, `session`,
`result_binding`, `result_binding_bytes`, and `result_binding_sha256` exactly.
Before copying, the current pointer, generation receipt, payload
inventory/manifest, and semantic-input file are reopened and verified through
existing read-only generation authority. The pointer's generation ID must equal
`carried_source_generation_id`, its receipt digest must equal the structural
request's expected current receipt digest, and the semantic-input bytes and
inventory entry must agree with that receipt's payload manifest. Arbitrary
historical generations, orphan handoff files, or directory scans are forbidden.
The carried entry may not replace worker evidence with a prior receipt or a
newly synthesized terminal.

Each `MATERIALIZED_ENTRY` has exactly `work`, `work_sha256`, `payload`,
`payload_bytes`, `payload_sha256`, and `result_binding_sha256`. It is derived,
never caller-selected. Its payload is copied exactly from the selected result
envelope and follows the accepted worker's operation-matched grammar. The
record's `results` array is ordered by NFC-normalized path in lexicographic
UTF-8 byte order, then ascending desired revision, operation, content digest,
and result-binding digest. `materialized` is ordered only by path in the same
byte order and contains at most one final `UPSERT` slot for each path.

The exact canonical record size must not exceed the structural request's
`expected_payload_bytes`. `GenerationStore` remains the owner of shared capacity
truth. This successor's implementation must extend its trusted usage scan for
this preflight and every later generation allocation to no-follow enumerate all
retained canonical files below
`workspaces/<repo_uuid>/semantic-staging/handoffs/<target_generation_id>/` and
add their exact byte sizes to the same `(repo_uuid, target_generation_id)` usage
key. If that target also exists in generation, staging, or quarantine storage,
the byte totals are summed and the target consumes one generation slot, not two;
a handoff-only target also consumes one slot. All retained handoffs remain in
that shared usage until a separately authorized cleanup or GC removes them.

Before first installation, the matching caller-supplied `CapacityPolicy` is
revalidated and a conservative preflight counts existing shared usage, the full
target-generation reservation, and the new handoff bytes against the
workspace/global byte and generation ceilings plus filesystem reserve. Exact
replay counts every already installed file once and adds no duplicate bytes or
generation slot. Unsafe names or types, unreadable bytes, overflow, or a usage
scan that cannot stabilize fails closed as capacity uncertainty. The later
ordinary generation allocation repeats this extended shared scan after the
handoff is visible. Capacity failure installs no new handoff; later allocation
failure leaves only the capacity-counted immutable handoff as classified
recovery evidence and creates no generation payload or queue bind. No
environment or implicit size default may increase any bound.
Every individual begin, public result, result envelope, payload, semantic text,
semantic ID, collection, and queue count retains its already-frozen worker or
queue limit.

Installation occurs before `request_staged_build()`. The exact locked authority
snapshot plus the complete accepted result set is install authority for this
immutable evidence file; it grants no lease, queue transition, staged-build
transition, certification, or cleanup authority. Install-once semantics apply:
the exact same canonical bytes are idempotent and different bytes at the same
derived path are a conflict. After a possible install fault, a no-follow reopen
that proves the exact expected bytes, mode, size, and digest adopts the commit;
proven absence may retry only while the retained authority snapshot is still
exact; different, unreadable, unsafe, or ambiguous state is commit-unknown and
blocks every downstream write.

Legacy completed queue items, raw result envelopes, and generations created
before format version 1 remain readable under their accepted contracts but are
not silently adopted. If an exact desired work item has neither a fresh accepted
session nor an exact entry in the verified current generation's version-1
semantic-input file, this child fails closed. It does not search arbitrary
history, reset completion, manufacture a terminal, migrate queue state, or infer
an association from a watermark.

### Deterministic `UPSERT` and `DELETE` materialization

Materialization in this child means the exact generation-owned semantic input
set, not a query-visible graph merge. It never invokes provider discovery,
`graphify.llm`, fuzzy/entity deduplication, `build_merge()`, clustering, query,
or publication. Starting from an empty path-keyed semantic map, it applies the
validated result entries in the record order above:

- `UPSERT` requires `operation="UPSERT"` and a `semantic_fragment` payload. It
  replaces the slot for that exact path with the current work, payload, payload
  digest, and result-binding digest.
- `DELETE` requires `operation="DELETE"` and the exact kind-only
  `delete_tombstone` payload. It removes the slot for that path. Deleting an
  absent slot is deterministic and successful; the tombstone remains in
  `results` even though it produces no `materialized` entry.

Ascending desired revision therefore determines same-path replacement. The
existing reconciliation rule already forbids two operations for the same path
and desired revision. Any mismatch between operation and payload, nonascending
same-path revision, duplicate desired identity, multiple final slots, or
caller-supplied `materialized` value that differs from recomputation is a
conflict. No last-writer rule outside this ordering, path aliasing, case folding,
ID remapping, deduplication, or best-effort omission is permitted.

After the request-bound `BUILD` lease is acquired, allocation is accepted, and
`prepare_staged_build()` has produced the empty target-generation staging root,
the structural adapter writes its existing output. The handoff record's exact
canonical bytes are then installed without following links at:

```text
workspaces/<repo_uuid>/staging/<target_generation_id>/
  graphify-out/semantic-inputs.json
```

The copy is a single-link regular `0600` file and must be reopened and match the
external handoff byte-for-byte. The generation may contain no alternate
semantic-input file, sibling semantic payload, unrecorded result, or extra
staging root. Existing payload inventory rules reject links, special files,
path escapes, duplicate paths, and files outside `graphify-out`. The structural
payload bytes remain otherwise unchanged by this child.

The semantic-input record is private generation staging and may contain the
accepted bounded `label` and sanitizer-produced `rationale` text. Worker
sanitization is structural and syntactic validation, not content-level DLP.
Neither materialization, payload inventory, hashing, staged completion, nor
sealed-input binding releases that prose or asserts that it is secret-free,
non-verbatim, publication-safe, or query-safe.

### Staged completion, sealed binding, and exact replay

The forward order is exact:

1. validate the exact existing `SyncRequest`, distinct target and optional
   carried-source generation bindings, and complete result set, then
   install/reopen the immutable handoff;
2. durably install the exact structural staged-build request for that unchanged
   target generation;
3. acquire the request-bound `BUILD` operation, allocate that target generation,
   and prepare its empty staging root;
4. build the structural output and install/reopen the exact
   `graphify-out/semantic-inputs.json` copy;
5. obtain two fresh equal trusted source observations matching the structural
   request and handoff, then call `complete_staged_build()`;
6. recompute `payload_manifest_sha256("graphify-out", entries)` from the exact
   returned sorted inventory and require it to equal the durable staged
   `COMPLETE` manifest; and
7. under the same current `BUILD` grant, revalidate the repository, active
   source, operation and migration authority, the entire captured pre-bind
   queue revision/hash/policy/compaction/watermark/reconciliation snapshot,
   handoff, target-generation-owned copy, and staged manifest, then call
   `bind_sealed_inputs()` with that manifest digest.

`bind_sealed_inputs()` is the final mutation in this child. An existing null
binding may advance exactly once; an existing identical digest is idempotent;
an existing different digest is a conflict. The returned queue must be reopened
and prove the same reconciliation and exact digest before the lease is
released. No certification request, generation receipt, journal certification,
promotion, pointer mutation, or public success receipt follows.

Crash recovery reuses the existing staged-build barrier. Before staged request
installation, only the immutable handoff may exist and exact replay begins from
it using the same target and carried-source identities. A different target,
source/target swap, or different carried source is a conflict, including after
an uncertain install. Once staged state exists, replay additionally requires the
same repository, target generation, and structural request in the exact
request-bound `REQUESTED`, `PUBLISHING`, or `COMPLETE` lifecycle; a certified
target or any other existing target state is not recovered by this child. The
null sealed-input value is required when the handoff is first installed; replay
of that already installed exact handoff may instead observe the one deterministic
post-bind queue state whose reconciliation differs only by the
expected manifest digest and queue commit revision/hash. `REQUESTED` or
`PUBLISHING` recovery reacquires only the exact request-bound operation; a
successor fence discards unsealed generation staging under the existing rules
and deterministically rebuilds/copies from the retained handoff. `COMPLETE`
recovery reopens the full payload inventory and adopts only the exact recorded
manifest. If the queue binding is still null and the entire captured pre-bind
snapshot remains current, the same digest may be bound; if the deterministic
post-bind state already equals that digest, the bind is adopted. A changed
reconciliation, newer desired set, unrelated queue revision, different sealed
digest, payload drift, missing handoff, or ambiguous durable state fails closed.

Uncertainty during the bind follows the same rule: exact reread of the expected
post-bind reconciliation and digest adopts; exact unchanged pre-bind state may
retry only under the same still-live grant; any other state is commit-unknown.
Manual file inspection, a staged `COMPLETE` record alone, or a queue watermark
alone never authorizes retry or success.

### Cleanup, failure routing, and audit evidence

Cleanup is subordinate to recovery evidence:

- the trusted workspace lifecycle composition owns only the best-effort
  deletion of an original consumed worker envelope described below. The
  accepted worker, semantic queue, and `GenerationStore` do not delete semantic
  staging on this child's behalf;
- an original consumed worker `result.json` becomes eligible for best-effort
  deletion only after the external handoff and its generation-owned copy have
  both been reopened, staged `COMPLETE` binds their exact bytes through the
  payload manifest, and the queue binding has been reopened with that digest;
- deleting an eligible consumed envelope is not part of the commit and cannot
  turn success into failure. The handoff itself remains retained through later
  certification or terminal abandonment, neither of which this child performs;
- conflicting, stale, foreign, extra, orphaned, legacy-unindexed, or
  commit-unknown staging is never automatically deleted or adopted. It remains
  quarantined evidence. A separately authorized semantic-staging repair or GC
  lifecycle, not this child, owns any later classification and deletion; and
- no cleanup may remove the only remaining copy of a worker transcript,
  result-binding payload, exact handoff, generation-owned semantic input, staged
  manifest, or queue binding needed to classify or recover an uncertain commit.

The implementation may use internal stable exception or audit classifications,
but this contract adds no public reason code, action code, result field, or
status-schema version. Existing public routing remains authoritative:

| Condition | Existing public truth/action |
|---|---|
| Pending, claimed, retrying, or watermark-gap work | `semantic_queue_pending` / `drain_semantic_queue` |
| Dead-letter work | `semantic_queue_dead_letter` / `inspect_semantic_queue` |
| Invalid queue or unsafe durable state | `semantic_queue_invalid` / `inspect_semantic_queue`, or the existing safe-state-root route |
| Nonterminal staged build | `staged_build_recovery_required` / `resume_exact_workspace_sync` |
| Source, reconciliation, pointer, operation, migration, compatibility, or request drift before a staged barrier | withhold the internal handoff and obtain fresh authoritative inputs; no new status field |
| Handoff or sealed-bind commit uncertainty | no inferred success and no new public terminal; exact durable reread is required |

Public status, command output, logs, and error text must not expose semantic
payloads, labels, rationales, source bytes, complete begin/session objects,
private paths, credentials, provider/model data, owner/fence identities,
environment values, or raw exceptions. Bounded internal audit evidence records
only the repository, target-generation, and optional carried-source-generation
identities, structural-request and handoff digests, result/materialized counts,
queue pre/post revisions and hashes, payload-manifest digest, lifecycle boundary,
and redacted classification.

Verification injects short writes, `EINTR`, `ENOSPC`, `EDQUOT`, `EIO`, failed
file and directory sync, failed replace, process death, and post-commit faults at
handoff installation/reopen, staged-request commit, staging preparation/reset,
semantic-input copy/reopen, staged completion, queue bind, and lease release.
It also covers every digest substitution; missing, duplicate, stale, foreign,
extra, legacy, and carried result; UPSERT/DELETE order and same-path revision;
capacity edges; symlink, hardlink, mode, special-file, and path escapes; payload
drift; content-redaction snapshots; and exact replay before and after each
durable boundary.

### Exact stop boundary

This child ends with a reopened staged `COMPLETE` payload manifest and an exact
reopened queue `sealed_input_manifest_sha256` equal to that digest. It grants no
public semantic-sync command, provider/backend execution, content-release or
DLP decision, query projection, generation certification, runtime receipt,
journal certification, promotion, pointer movement, migrate, repair, GC,
service/watch, publication, production/runtime installation authority,
performance/resource qualification, parent-phase completion, or successor
readiness. The separate governance receipt accepts only this bounded internal
child.

## P5B2 semantic-generation certification finalization

This unnumbered P5B2 child is implemented and accepted only at the frozen
internal boundary in this section. The
[accepted completion receipt](receipts/p5b2-semantic-generation-certification-finalization.md)
was made canonical by merged PR #58 and binds the exact PR #56 implementation
together with PR #57's corrective delivery. It does not revise the accepted
worker or handoff contracts and grants no public semantic-sync authority or
successor readiness.

The child composes only existing durable authorities:

- the canonical `SyncRequest` and its exact new target generation;
- the complete existing `StructuralBuildRequest` and request-bound staged-build
  record;
- the retained handoff plus target-generation-owned
  `graphify-out/semantic-inputs.json` from the accepted predecessor;
- `SemanticQueueStore.certification_view()` and the existing immutable
  `graphify.workspace.semantic_certification_binding.internal` format-version-1
  record;
- `GenerationStore.certify()`, the existing
  `graphify.workspace.generation_receipt` v1 record, generation coordination
  lock, lifecycle journal, capacity reservation, and staged-state transition;
  and
- the existing fenced lease acquisition, acceptance, release, and exact
  recovery rules.

No public request/result family, CLI argv, status field, JSON Schema, durable
format, completion index, or new receipt family is introduced.

### Exact start boundary

The only forward entry is the accepted semantic-result handoff's durable stop
boundary after its exact `BUILD` grant has been released. Before any new lease
or mutation, the trusted lifecycle composition must reopen and cross-check all
of the following:

1. the canonical accepted `SyncRequest`, its digest, repository UUID, and exact
   target generation;
2. one canonical staged-build format-version-1 record in lifecycle
   `COMPLETE`, with no receipt, pointer revision, or abandonment intent, bound to
   that repository, target, and complete `StructuralBuildRequest`;
3. the exact sorted target payload inventory, whose
   `payload_manifest_sha256("graphify-out", entries)` equals the staged
   `payload_manifest_sha256`, stays within the immutable request reservation,
   and contains the sole allowed target semantic input at
   `graphify-out/semantic-inputs.json`;
4. the retained external handoff at the target/request-derived path and the
   generation-owned semantic-input file, with byte-for-byte equality, exact
   handoff digest, `0600` regular single-link files, contained no-follow paths,
   and an inventory entry that binds the copy's exact size and SHA-256;
5. the current queue snapshot and complete semantic-required reconciliation,
   including queue policy, revision and canonical-state SHA-256, compaction
   epoch, equal positive desired/completed watermarks, exact desired set and
   observation evidence, no incomplete retained item, and
   `sealed_input_manifest_sha256` equal to the staged manifest; and
6. current installed compatibility plus registry, selected active source,
   workspace operation and migration state, pointer, policy, source
   commit/epoch, and two fresh equal typed source observations that match the
   structural request, handoff queue evidence, and target inventory.

The structural request's logical digest must still equal the complete canonical
`SyncRequest` digest. The current global registry revision and selected
repository entry must equal the request's expected registry and active-source
revisions and source identity; the workspace migration epoch must equal the
request expectation; and the pointer revision/current-receipt pair must still
equal its request CAS. The selected compatibility digest must equal both the
request and current `GenerationStore`, and the installed queue policy must equal
the captured handoff policy. The prior staged `COMPLETE` operation epoch and
fence remain historical staged-completion evidence only. They are not current
certification authority and are never copied into the new receipt.

Before reacquisition, the durable workspace operation epoch and fence high-water
mark must still equal the staged `COMPLETE` operation epoch and fence, and the
predecessor `BUILD` lease must be absent. Any intervening operation is drift.
The first successful recovery acquisition advances both values exactly once;
commit-unknown replay with the same attempt digest must reopen that same grant
rather than advance them again.

A missing or noncanonical request, handoff, queue, inventory, reservation,
source observation, or staged record fails closed. A target in `REQUESTED` or
`PUBLISHING` is still owned by the predecessor recovery lifecycle and is not
advanced here. A target already recorded as staged `CERTIFIED` is not admitted
to the mutating entry; only the exact terminal replay proof below may be
returned read-only. A `PROMOTED` or `ABANDONED` staged record, arbitrary final
generation, foreign workspace, source/target substitution, different request,
different manifest, duplicate generation location, unsafe path, or ambiguous
current/previous/pending state is a conflict or recovery barrier.

Prior handoff lease-release uncertainty must be resolved first through the
existing lease store: an observed release return or locked reread proving the
exact owner/fence absent permits entry; the exact unchanged live record may be
retried only under its existing liveness rule. Replacement, unreadable, or
ambiguous lease state never becomes absence or successor authority.

### Exact recovery lease and staged reconstruction

Because the request and staged `COMPLETE` record already exist, this child may
call only `GenerationStore.acquire_staged_recovery()` for the same repository,
target, and complete structural request. It may not call
`request_staged_build()` again, acquire a fresh unbound operation, choose a new
generation, or use `MIGRATE`, `PROMOTE`, or `POINTER_RECOVERY`. The returned
operation must be exactly the request-bound `BUILD` recovery lane with the
caller's new attempt digest. Its accepted grant supplies the current operation
epoch and fence and must retain the current registry, active-source, and
migration authority.

Lease transitions preserve the existing registry-before-workspace order.
Generation operations preserve workspace-before-existing-generation-lock order;
the composition never takes a generation lock around registry or workspace
acquisition. Current pointer, policy, compatibility, request, and source
evidence is rechecked after acquisition and before the first certification
mutation. Drift at this boundary releases or classifies the exact recovery
grant and blocks certification; it does not authorize abandonment or cleanup.

Under that grant, the existing `allocate()` call may only recover the exact
capacity reservation and target allocation named by the staged request. The
existing `prepare_staged_build()` and `complete_staged_build()` calls may only
reconstruct the `StagedBuildPreparation` and `StagedBuildCompletion` wrappers
from the `COMPLETE` record and exact inventory. Their `COMPLETE` path is
adoption, not publication: it must not reset or remove staging, invoke the
adapter, rewrite payload bytes, reinstall the semantic-input copy, append a new
payload entry, or change the staged revision or manifest. A missing reservation
is recoverable only when existing `GenerationStore` rules independently prove
an exact interrupted certification location; otherwise it is a conflict.

The reconstructed completion's repository, target, allocation, structural
request, canonical staged state, entries, and manifest must equal the reopened
entry proof. The target must occupy exactly one allowed certification location.
Before receipt installation that is the exact staging directory; after an
uncertain certification boundary it may instead be the exact final generation
or exact staging directory containing an existing receipt, but only the
existing certification recovery path may classify and adopt it.

### Exact certification request and durable order

With the same live `BUILD` grant, the composition obtains two fresh equal typed
source observations and calls `SemanticQueueStore.certification_view()` with
the structural source epoch, those observations, and the exact staged manifest.
The view must match the entry queue evidence and all of these exact values:

- repository UUID, current queue revision and canonical-state SHA-256;
- equal desired and completed watermark;
- compaction epoch;
- source epoch and commit;
- policy, observation-manifest, and observation-evidence digests;
- the staged `sealed_input_manifest_sha256`; and
- `semantic_completeness="complete"`.

`not_required`, queue absence, a null or different sealed digest, scalar
watermark evidence, an incomplete item, an unequal or untrusted observation
pair, or a view from another queue revision is not admissible.

The exact `CertificationRequest` is derived, not caller-invented:

```text
source_commit                  = certification_view.source_commit
source_epoch                   = certification_view.source_epoch
policy_sha256                  = certification_view.policy_sha256
observation_manifest_sha256    = certification_view.observation_manifest_sha256
queue_watermark                = certification_view.queue_watermark
semantic_completeness          = "complete"
compatibility_sha256           = current request-selected compatibility
validations                    = {
  "coordination_lock_precreated",
  "payload_manifest",
  "stable_semantic_queue"
}
```

The validations are exactly that set; their canonical receipt representation
uses existing sorted-array behavior. The declared entries are exactly the
reconstructed completion entries. The target generation and structural request
remain supplied through the allocation and `staged_completion`; no new request
field or digest substitutes for them.

`GenerationStore.certify()` is the sole forward mutation authority and retains
this exact order:

1. under registry then workspace authority, require the exact allocation and
   staged `COMPLETE` proof, prevalidate the certification request and declared
   manifest, and look up any immutable certification binding at
   `queue/certifications/<target_generation_id>.json` by target,
   certification-request SHA-256, and sealed manifest;
2. when no binding exists, independently reobserve the selected source twice,
   obtain the existing semantic certification view, require it to equal the
   request, and then under the workspace lock revalidate the view's exact queue
   revision and state SHA-256;
3. before any generation lock or staged receipt becomes authority, install and
   reopen the canonical immutable binding of repository, target generation,
   certification-request digest, and complete queue view. Same bytes are
   idempotent; any different target, request digest, view, or manifest conflicts;
4. only after that binding is durable, take the pre-created target generation
   lock and let existing certification recovery advance the journal through
   `BUILT` and `VALIDATING` as required, inventory and sync the exact payload,
   install or reopen the existing generation receipt, atomically move the exact
   staging directory to the final generation location when still needed,
   reopen and verify the installed generation plus its semantic binding, and
   append or reopen the matching `CERTIFIED` journal event with pointer revision
   zero;
5. under registry/workspace authority, clear only the exact target capacity
   reservation, reopen the installed generation and receipt, and require the
   same canonical receipt returned by certification; and
6. under the existing generation lock, durably advance the same staged record
   by one revision from `COMPLETE` to `CERTIFIED`, preserving its repository,
   target, structural request, and payload manifest while recording exactly the
   verified receipt SHA-256 and the receipt's new operation epoch and fence.

The existing receipt is the only runtime receipt. It must name the same target,
source commit/epoch, active-source revision, policy, observation manifest,
queue watermark, `semantic_completeness="complete"`, compatibility, payload
entries and manifest, coordination lock, current recovery operation epoch and
fence, and exact validation set. No field is supplied from the predecessor's
old `COMPLETE` fence, from a caller-preseeded receipt, or from newer unbound
queue state.

### Replay, drift, and uncertainty

Before the immutable certification binding exists, any queue revision/hash,
source, policy, active-source, registry, pointer, migration, operation,
compatibility, request, target, manifest, inventory, handoff, generation-copy,
or source-observation drift blocks new certification. The child does not update
the request, rerun reconciliation, rebind staged inputs, rebuild payloads,
select another target, or weaken `semantic_completeness` to continue.

A durable exact binding is the commit boundary for the captured queue view. If
its install return was uncertain, only reopening the same canonical target,
request digest, view, and manifest adopts it. Proven absence may retry while all
pre-binding authority remains exact. Different, unreadable, unsafe, or
ambiguous binding state is commit-unknown. Once that exact binding is durable,
a newer current queue is not adopted into the certification; the existing
`GenerationStore` recovery path may finish only the already-bound request and
view. That is recovery of prior authority, not a new certification from drifted
state.

A durable exact staged or installed generation receipt, verified against that
binding and a matching `VALIDATING`/`CERTIFIED` history, is the next recovery
boundary. Receipt installation, staging-to-final rename, installed-generation
verification, or journal uncertainty may be adopted only by the existing
`GenerationStore` exact reread and recovery checks. A preseeded receipt without
the binding, different receipt bytes, receipt without matching payload or
journal authority, both or neither generation locations, or any ambiguous
suffix remains a conflict. After the exact receipt is durable, later source,
policy, compatibility, or queue change is never copied into it; only that same
receipt may finish journal or staged-state recovery.

Staged-state uncertainty is resolved by recovering the canonical durable
record. Exact `CERTIFIED` with the expected previous `COMPLETE` identity,
request, manifest, and receipt is adopted. Exact `COMPLETE` plus an exact
binding and durable receipt may use only
`GenerationStore.recover_staged_certification()` or the equivalent exact
`certify()` recovery to finish the marker. Any other lifecycle, revision jump,
receipt, request, manifest, abandonment intent, pending state, or ambiguity is
not success.

Reservation-clear uncertainty requires an exact recovered capacity-state reread.
The target reservation's proven absence is success for that boundary; the exact
unchanged reservation may be cleared idempotently under the same recovery
authority. A different reservation, uncertain recovery state, or unrelated
capacity mutation is not absence and may not be cleaned up by this child.

Lease release remains cleanup rather than certification acceptance. The release
return or a locked durable reread proving the exact recovery owner/fence absent
is required before terminal success. If a later invocation reopens the full
exact staged `CERTIFIED` proof but the certification attempt's paired `BUILD`
lease and staged-attempt digest remain durable, it may enter one cleanup-only
recovery lane. That lane adopts the persisted attempt digest, reopens the exact
unchanged live grant for the current OS owner, or replaces only an expired or
rebooted grant under the existing fence and liveness rules. It may verify the
terminal proof, release that cleanup grant, and reread absence; it may not call
`certify()`, rewrite the staged record or receipt, clear another reservation, or
perform any other lifecycle mutation. The certification epoch and fence remain
the values already frozen in the staged record and receipt; a replacement
cleanup epoch or fence is never copied into them. The complete immutable proof
is checked before acquisition and rechecked under the cleanup grant. If the
second check fails, the exact cleanup grant is still released before returning
the primary failure unless release itself becomes uncertain. A live foreign owner,
different operation, missing or different attempt digest, replacement
authority, unreadable state, or any ambiguity is busy, conflict, or
commit-unknown rather than cleanup authority. A release failure never rewrites
the receipt or staged state and never permits inferred success.

Exact same-byte/state replay at any preterminal boundary is idempotent and must
produce the same binding, receipt, installed generation, journal event,
reservation state, and staged revision. An invocation that begins after the
full exact staged `CERTIFIED` proof and cleanup-grant absence already exist
performs read-only terminal verification and returns that same internal proof
without acquiring a `BUILD` lease or mutating state. The retained-grant case
above is the sole exception and grants cleanup only. A merely existing final
directory, a different certified generation, or a `PROMOTED` target is not
terminal replay authority.

No failure or recovery path resets or deletes staging, abandons the target,
removes the handoff, semantic-input copy, receipt, binding, journal evidence, or
installed generation, compacts or rewrites the queue, or performs semantic
staging cleanup. There is no inferred success from a directory, receipt,
watermark, manifest, journal head, absent reservation, or absent lease alone.

### Exact stop boundary

To reach terminal success, the composition must durably assemble and
cross-check one terminal proof. While the recovery grant is still live and
before releasing it, the composition proves the first five items below. It then
releases that exact grant and proves the final item by locked durable reread:

- the staged record is exactly `CERTIFIED`, still binds the same repository,
  target, structural request and manifest, records the exact verified receipt
  digest, has no pointer revision or abandonment evidence, and uses the
  receipt's certification epoch and fence;
- `GenerationStore.verify_generation()` reopens one final target generation,
  reproduces the exact payload inventory and manifest, validates the retained
  coordination lock, receipt, `semantic_completeness="complete"`, queue
  watermark, compatibility, source/policy/observation facts, and immutable
  semantic certification binding;
- the certification binding reopens at the target-derived path and equals the
  exact certification-request digest, queue view, and sealed manifest;
- the lifecycle journal contains the matching `CERTIFIED` event for that target
  and receipt with pointer revision zero, without a promotion transition owned
  by this child;
- the exact target capacity reservation is durably absent and the visible
  pointer remains at the entry request's revision/current-receipt boundary; and
- after release, the exact recovery owner/fence is durably absent.

Only that complete proof ends the child. It is internal and adds no public
success receipt or status field. The target remains a certified but unpromoted
generation behind the existing staged recovery barrier.

This stop grants no content-release or DLP decision for retained labels or
rationales, graph construction/merge/query projection, promotion, pointer
movement, public semantic-sync command, provider/backend/model selection,
credentials, networking, migrate, repair, GC, cleanup, service/watch,
publication, production/runtime installation authority, performance/resource
qualification, P5C, H3, P6+, parent-phase completion, successor readiness,
governance acceptance, or merge authority.

## P5B2 semantic-generation promotion and pointer-finalization

This unnumbered P5B2 child is implemented and accepted only at the frozen
internal boundary in this section. The
[accepted completion receipt](receipts/p5b2-semantic-generation-promotion-finalization.md)
binds the exact PR #59 through PR #64 delivery and correction chain. Focused
regression coverage exercises direct promotion, exact-current
replay, pointer and staged recovery, every documented acquisition and release
commit boundary, substitution rejection, semantic-evidence preservation, and
exact terminal cleanup. Acceptance introduces no
public command, request/result family, schema, durable format, runtime receipt,
or provider/backend path and activates no later successor. The receipt remains
repo-local evidence; merged PR #65 made its governance acceptance canonical.

The child composes only existing authorities:

- the canonical `SyncRequest`, complete `StructuralBuildRequest`, and exact
  staged-build record already accepted as `CERTIFIED`;
- the installed target generation, canonical generation receipt, coordination
  lock, lifecycle journal, immutable semantic certification binding, and
  retained semantic-result handoff;
- `GenerationStore.acquire_staged_recovery()` and
  `GenerationStore.complete_staged_promotion()`;
- the existing `PointerCAS`, `PointerStore.promote()`, pointer-recovery
  projection, and `PointerStore.recover()` authorities; and
- the existing registry, selected-source, workspace lease, migration,
  compatibility, queue-policy, state-schema, and lock-order contracts.

### Exact start boundary

The only admissible entry is the predecessor's complete accepted terminal
proof after certification cleanup is finished. Before lease acquisition or any
pointer mutation, the composition must reopen and cross-check all of the
following as one proof:

1. one canonical accepted `SyncRequest`, its digest, repository UUID, exact
   target generation, and complete canonical `StructuralBuildRequest` whose
   logical request digest equals that `SyncRequest`;
2. one canonical staged-build format-version-1 record in lifecycle
   `CERTIFIED`, bound to that repository, target, request, and exact payload
   manifest, with the exact generation-receipt SHA-256 and certification
   operation epoch/fence, no pointer revision, and no abandonment intent or
   evidence;
3. exactly one installed target generation whose reopened payload inventory,
   manifest, receipt, compatibility, source/policy/observation facts,
   `semantic_completeness="complete"`, validation set, and retained
   coordination lock reproduce the staged record and accepted certification
   proof;
4. the target-derived immutable semantic certification binding, reopened with
   the same certification-request digest, complete queue view, sealed-input
   manifest, and exact installed receipt;
5. a recovered lifecycle journal with no pending journal action and exactly one
   matching target `CERTIFIED` event whose receipt is exact and whose pointer
   revision is zero, with no target `PROMOTED`, `REPAIRED`, `SUPERSEDED`, or
   failed terminal event;
6. durable absence of the exact target capacity reservation and of every
   certification `BUILD` recovery grant and staged-attempt digest;
7. no durable pointer intent, plus a visible pointer that is either absent for
   request CAS `(revision=0, current_receipt=null)` or exactly equals the
   request's expected pointer revision and current-receipt SHA-256; and
8. current installed compatibility and queue policy plus the durable registry,
   selected-active-source, migration-epoch, state-schema, and certified source
   commit/epoch evidence that still agree with the request and receipt; before
   a new direct pointer move, two fresh equal typed source observations must
   also reproduce the frozen structural observation evidence.

Items 1--8 define fresh forward entry. A later invocation recovering after
acquisition or a pointer write began may vary only the clauses whose fresh
state was absence: item 5 may contain only the exact target-bound `PROMOTED` or
`REPAIRED` journal residue of that move; item 6 may retain only the exact
persisted promotion attempt and its live grant, or proof that the grant is
expired or rebooted and eligible for request/target-bound replacement; and item
7 may contain only the exact pending or already-visible pointer residue. The
target reservation and certification `BUILD` grant must remain absent, and
every other part of items 1--8 must remain exact. Recovery preserves the same
staged `CERTIFIED` record, installed generation, receipt, binding, request and
target and proves the residue's registry/active-source and pointer authority.
That is recovery of the prior move, not a second forward entry. Once the exact
pointer intent or exact target visibility is durable, recovery does not require
the selected checkout to remain observable and must not recapture or adopt
newer source observations.

The staged certification epoch/fence remains immutable certification evidence.
It is not copied forward as a pointer-mutation grant. The current workspace
operation epoch and fence high-water mark may be later only when the accepted
certification cleanup advanced them without changing the certified receipt;
their exact terminal values are captured and the next grant advances from
current state. A merely certified directory, receipt, binding, journal event,
absent reservation, absent lease, or matching pointer is not entry authority
by itself. Missing, duplicated, foreign, substituted, incompatible, unsafe,
unreadable, or ambiguous evidence fails closed. A staged `COMPLETE`,
`PROMOTED`, or `ABANDONED` record, a different certified generation, a newer
request, or an already-drifted pointer is outside forward entry.

### Request-bound acquisition and forward decision

The composition may acquire mutation authority only through
`GenerationStore.acquire_staged_recovery()` for the same repository, target,
and structural request. A fresh acquisition creates one new caller attempt
SHA-256 and retains it across an acquisition commit-unknown retry. Recovery
with a persisted promotion attempt must adopt that exact digest rather than
invent another one. It may not call `request_staged_build()`, acquire a generic
workspace operation, reopen `BUILD`, select another target, or use `MIGRATE`,
`REPAIR`, `ROLLBACK`, `GC`, or abandonment as a promotion substitute.

The existing staged-recovery classifier owns the operation:

- with no `pointers.pending.json`, the grant must be `PROMOTE`; and
- with a durable pointer intent, the grant must be `POINTER_RECOVERY`.

Any other operation is a conflict. The current OS owner may reopen the same
live grant only with the retained attempt digest. After exact expiry or reboot
proof, request/target-bound acquisition may replace that grant with the same
persisted attempt and the operation classified from the durable pointer state;
the replacement epoch/fence is current mutation authority, not proof that an
earlier boundary completed. Proven grant and attempt absence may retry from the
unchanged fresh entry with one new digest. A different digest while an attempt
remains, a live foreign owner, an operation not selected by the classifier, a
different request or target, or ambiguous replacement authority is busy,
conflict, or commit-unknown, never successor authority.

Registry-before-workspace ordering remains mandatory for acquisition and grant
validation. Pointer movement retains workspace authority before taking the
sorted exclusive generation-lock set for the exact candidate and any visible
current/last-good references. A generation lock never wraps registry or
workspace acquisition. After acquisition and immediately before a new direct
pointer write, the composition revalidates the full fresh-entry proof, accepted
grant, liveness, and two equal current source observations. An exact pending or
already-visible recovery path instead revalidates the durable recovery proof,
grant, registry/active-source authority, candidate receipt and pointer plan
without consuming source payload or requiring source observation. Drift
releases or classifies that exact grant and blocks this child; it does not
update the request, adopt newer authority, substitute a target, or infer
success.

### Direct exact promotion

Under a `PROMOTE` grant, an already-visible candidate is admissible only when
the current reference equals the exact target generation and certified receipt
and its pointer/journal proof is complete. That path performs no new pointer
move and proceeds only to staged finalization. Any other already-advanced
pointer is a conflict.

For a new move, the only allowed `PointerCAS` is derived from the frozen request
and accepted grant:

```text
expected_pointer_revision         = request.expected_pointer_revision
expected_current_receipt_sha256   = request.expected_current_receipt_sha256
expected_active_source_revision   = accepted grant active-source revision
expected_source_epoch             = certified receipt source epoch
expected_operation_epoch          = accepted grant operation epoch
expected_migration_epoch          = accepted grant migration epoch
expected_state_schema_version     = current frozen state schema version
expected_fence_token              = accepted grant fence token
candidate_generation_id           = exact certified target generation
candidate_receipt_sha256          = exact certified target receipt SHA-256
```

Every field is mandatory. The candidate must reopen through
`GenerationStore.verify_generation()` and remain eligible under the exact
`CERTIFIED` journal event. `PointerStore.promote()` is the sole direct pointer
writer. Its existing durable order is retained: preserve the verified prior
pointer when any; install the exact pending pointer intent; make that exact
pointer visible; append the authoritative `PROMOTED` journal event; then unlink
and sync the pending intent. The direct pointer revision is exactly the prior
visible revision plus one. A stale CAS may append only the existing
`SUPERSEDED` result for the losing exact candidate and is never promotion
success. The entry proof requires all referenced generations to be valid, so
this child has no corrupt-generation quarantine or generic repair authority.

### Exact pointer recovery

When durable pointer intent selects `POINTER_RECOVERY`, it may reconcile only
pending or already-visible residue from the same request's exact target/receipt
move. Before mutation, the locked recovery projection and any supplied
`expected_plan` must prove all of the following:

- the verified candidate is the same target generation and receipt;
- the selected source is the exact target-bound pending or visible move, not a
  prior, last-good, unrelated current, arbitrary certified, or newly discovered
  generation;
- the pending/visible relationship, retained prior revision, registry and
  active-source revision, operation and migration authority, source epoch,
  compatibility, generation receipt, certification journal, and pointer
  revision chain are valid;
- no corrupt-generation quarantine, rollback, GC, public `REPAIR`, or other
  lifecycle action is part of the plan; and
- re-deriving the plan under the mutation locks produces the same decision.

A compatible exact residue may be resumed or finalized at its existing
revision. When the existing pointer store must re-emit that same target and
receipt at a later monotonic revision to close visible commit uncertainty, the
new revision and authoritative `REPAIRED` event are recovery evidence only;
they do not authorize target substitution or adoption of unrelated newer
state. A plan whose candidate, selected source, last-good, quarantine set, or
pointer facts differ is rejected before pointer mutation. Corrupt, stale,
incompatible, foreign, or ambiguous pending state remains a recovery barrier.

`PointerStore.recover()` remains the sole recovery writer. Its returned pointer
is not accepted merely because it is readable. It must bind current to the
exact target/receipt, have the exact recovered revision, operation epoch and
fence, have a matching authoritative `PROMOTED` or `REPAIRED` journal event,
and leave no pending pointer intent. A generic no-op or replacement that merely
finds some valid current generation is outside this child.

### Staged pointer finalization

After either path returns the exact visible pointer, and only after pending
intent absence is durably proved, the composition calls
`GenerationStore.complete_staged_promotion()` with the same staged-recovery
attempt and exact pointer. That method may only:

1. reopen the exact request-bound staged record in `CERTIFIED` or exact replay
   `PROMOTED`;
2. require the visible pointer bytes to equal the supplied pointer and current
   to bind the same repository, target generation, receipt, active-source
   revision, and source epoch;
3. require the pointer revision to exceed the original request CAS and reject
   pointer authority from a fence newer than the accepted grant;
4. under the existing target generation lock, reopen the unchanged installed
   generation and exact receipt and require the authoritative target
   `PROMOTED` or `REPAIRED` journal event at the same pointer revision,
   operation epoch, and fence; and
5. advance the same staged record by exactly one durable revision from
   `CERTIFIED` to `PROMOTED`, preserving its repository, target, structural
   request, payload manifest, and receipt while recording that exact pointer
   revision and pointer-authority epoch/fence.

The method never moves or repairs a pointer. An uncertain staged-state commit
is adopted only when durable recovery reopens the exact expected `PROMOTED`
record with the exact previous `CERTIFIED` identity and pointer proof. A
revision jump, different receipt, changed manifest/request, pending staged
record, abandonment evidence, or mismatched pointer is not terminal success.

Before and after staged finalization, the composition reopens the installed
payload inventory, generation receipt, semantic-input bytes, immutable semantic
certification binding, and retained handoff. Their canonical bytes and digests
must equal the start proof. Promotion changes pointer and staged lifecycle
evidence only; it does not rebuild, recertify, rewrite, release, project, or
clean semantic content.

### Commit uncertainty, replay, and release

Each durable boundary is independently recovered:

- acquisition uncertainty reuses only the retained attempt digest and exact
  live grant, or after expiry/reboot proof replaces it through the same
  request/target-bound classifier without changing that digest;
- pending-intent uncertainty adopts only the exact target-bound intent or
  enters exact `POINTER_RECOVERY`;
- visible-pointer uncertainty never succeeds before matching journal authority
  and pending-intent resolution;
- journal uncertainty reopens only the exact target, receipt, revision,
  operation epoch, fence, and `PROMOTED` or admissible `REPAIRED` transition;
- staged-transition uncertainty reopens only the exact `PROMOTED` successor of
  the accepted `CERTIFIED` record; and
- release uncertainty retries only the exact unchanged live grant or adopts a
  locked durable reread proving that exact owner/fence and staged-attempt digest
  absent without replacement authority.

Same canonical state is idempotent. An invocation beginning with the complete
terminal proof and exact grant absence performs read-only replay verification.
If the pointer and staged transition are complete but the same live promotion
grant and staged-attempt digest remain, a cleanup-only path may verify the full
terminal proof, adopt that exact persisted attempt, release only that grant,
and reprove absence. The same live grant is reusable only by its current OS
owner; an expired or rebooted grant may be replaced only through a
request/target-bound terminal-cleanup acquisition that first verifies the exact
`PROMOTED` proof. A generic unbound lease acquisition is forbidden. Any cleanup
replacement epoch or fence is cleanup authority only and is never copied into
the pointer, journal, receipt, or staged `PROMOTED` record. Cleanup may not
move/recover the pointer, rewrite staged state, or recertify. A live foreign
owner, different attempt or operation, invalid terminal proof, later unrelated
fence, or ambiguous/unreadable state is not cleanup or absence. No success is
inferred from an installed generation, visible target, journal head, absent
pending file, `PROMOTED` marker, or released lease alone.

No replay or failure path selects a different generation, adopts a newer
request or pointer, deletes or resets staging, removes the handoff,
semantic-input copy, binding, receipt, journal, or installed generation,
rewrites the queue, or performs semantic-staging cleanup.

### Exact stop boundary and carried evidence

Terminal success requires one locked, cross-record proof:

- the staged record is exactly `PROMOTED` for the same request, target,
  manifest, and receipt, and records the exact pointer revision plus
  pointer-authority operation epoch and fence;
- the visible pointer current is bound to that exact target and receipt, its
  revision equals the staged revision evidence and exceeds the request CAS, and
  the lifecycle journal contains the matching authoritative `PROMOTED` or
  admissible `REPAIRED` event;
- no unresolved `pointers.pending.json` or journal recovery action exists;
- the installed payload inventory, semantic-input bytes, generation receipt,
  coordination lock, retained handoff, and immutable semantic certification
  binding are unchanged from the accepted entry proof; and
- after cleanup, a locked durable reread proves the exact acquired promotion
  owner/fence and staged-attempt digest absent without replacement ambiguity.

Only the exact generation proved both staged `PROMOTED` and visible current by
that terminal proof may later be offered as carried semantic-result evidence
to a separately authorized handoff. This boundary neither invokes that handoff
nor makes its retained labels or rationales releasable, query-safe, DLP-cleared,
semantically correct, or publication-ready.

Content release or DLP decisions, graph construction/merge/query projection,
changes to `query_structural()`, public semantic-sync commands, schemas,
runtime formats, runtime receipts, providers/backends/models, credentials,
networking, migrate, repair, GC, service/watch, publication, production/runtime
installation, performance/resource qualification, P5C, H3, P6+, parent
completion, execution authority, and later-successor readiness are explicitly
excluded. P5 and P5B2 remain `IN_PROGRESS`; H3 remains `DEFERRED`; this child
alone transitions from `READY` to `COMPLETE`; remaining P5B2 work and P5C work
remained `WAITING` and no later successor was `READY` at that acceptance point.
The separately accepted trust-root prerequisite recorded below does not expand
or reopen this accepted boundary.

## P5B2 semantic-release bundle and deterministic-classifier trust-root

This separate unnumbered P5B2 prerequisite subchild is implemented and accepted
only at this frozen boundary. Its
[acceptance receipt](receipts/p5b2-semantic-release-trust-root.md) binds the
exact PR #66 contract freeze, PR #67 readiness reconciliation, PR #68
implementation, and PR #69 C1-control repair. It has no content-release,
publication, execution, parent-completion, or successor authority. Its entire
scope is the repo-owned installed package-data trust root plus the existing
installed executable bootstrap needed to establish that root before Graphify
imports:

- `graphify/workspace/semantic_release_manifest.json`;
- the manifest-inventoried deterministic classifier implementation and
  byte-defined classifier ABI;
- the closed taxonomy, text-normalization contract, and ordered ruleset;
- the required explicit-evidence-only `core_secrets.v1` profile; and
- every repo-owned selectable coverage-profile artifact and its manifest-bound
  identity, version, bytes, size, mode, and SHA-256; and
- the source-executed installed private `_graphify-semantic-authority` and
  `_graphify-mcp-semantic-authority` bootstrap scripts
  whose POSIX shell prelude executes installed Python with `-S` isolation before
  Python startup hooks can run, excludes package-local bytecode caches from
  trusted installed executable imports, suppresses `.pth`, `sitecustomize`, and
  automatic user-site startup imports, and may then add the installed
  script-prefix package root, or a PEP 610 editable source root recorded by a
  `graphifyy` direct URL in that same script prefix, explicitly before any
  Graphify module is loaded. Owner discovery covers every supported
  script-prefix site-packages layout as one set: after real-path
  deduplication, exactly one physical or recorded editable Graphify owner must
  remain. Zero owners and multiple distinct owners fail before import;
  interpreter site roots never substitute for a missing script-prefix owner.

The manifest and referenced artifacts are never caller, workspace, operator,
provider, backend, model, credential, network, environment, or policy input.
This accepted subchild does not self-authenticate the instructions that establish its
first import boundary. The CPython executable and standard library, an
installer that verifies the selected wheel and its `RECORD`, the installed
source-executed bootstrap bytes, and post-install filesystem protection of the
bootstrap and package source are explicit trusted-computing-base prerequisites.
The wheel `RECORD` binds both bootstrap scripts and the classifier source; it is
the external installer and operating environment, not classifier code that has
already started executing, that must reject source or launcher substitution.
Package-local bytecode caches remain outside that prerequisite and are ignored
by the installed executable path.

The public `graphify` and `graphify-mcp` console entry points remain ordinary
cross-platform package commands and are not semantic-release decision authority.
On the supported POSIX boundary, an authority-qualified uv installation uses
`uv tool install --force --reinstall --link-mode copy graphifyy`, including when
requalifying an existing ordinary installation. Qualification then verifies the
selected wheel and `RECORD`, exact bootstrap and bundle bytes, exact final modes,
and single-link regular-file state. A hardlinked or wrong-mode final installation
remains an ordinary package installation, but it is not authority-qualified and
bundle loading must fail closed; installer history or umask alone is not an
authority fact.
The exact installed-root, path-normal-form, descriptor-relative no-follow,
single-link regular-file, mode, size, digest, deterministic-pattern-only ABI,
`utf8_lex_v1`, taxonomy/profile separation, `core_secrets.v1`, hard-limit, and
fail-closed contracts are the canonical requirements in
[Classifier trust root and installed bundle](#classifier-trust-root-and-installed-bundle)
and
[Closed classification and coverage contracts](#closed-classification-and-coverage-contracts).
Those requirements belong to this prerequisite even though the encompassing
decision child composes their eventual implementation.

This subchild implements only installed-bundle loading and validation, the
manifest-bound deterministic factual classifier over explicit,
already-canonical bounded UTF-8 bytes, and the existing installed executable
bootstrap needed to start Python under isolation flags, set a fresh
package-external pycache prefix, suppress Python startup hooks and automatic
user-site startup imports, and add only installed package roots before Graphify
imports. It does not select profiles for a
workspace or release context, interpret `NO_MATCH` as release safety, map
categories to dispositions, read semantic-generation state, or persist any
record. Its stop boundary excludes operator policy-authority provisioning,
`SemanticReleaseDecisionStore`,
capacity or GC integration, semantic-input field/inventory composition, policy
reduction, omission execution, projection, new public CLI/schema/receipt
surfaces, provider/backend behavior, publication, and every broader release/DLP
decision.

P5 and P5B2 remain `IN_PROGRESS`; the trust-root and policy-authority
provisioning prerequisites are `COMPLETE`. The encompassing semantic-content
release/DLP decision child,
remaining P5B2 work, and P5C remain `WAITING`; H3 remains `DEFERRED`; no later
successor is `READY`. Acceptance is limited to the bounded internal prerequisite
and invents no operator-policy provisioning or decision-store behavior.

## P5B2 semantic-release policy-authority provisioning prerequisite

This separate internal unnumbered P5B2 prerequisite is implemented and accepted
as `COMPLETE` at this frozen private boundary. Completion evidence is the
[`P5B2 semantic-release policy-authority` receipt](receipts/p5b2-semantic-release-policy-authority.md),
which binds PR #71's contract freeze, PR #72's implementation delivery, and PR
#74's lock-discipline correction. It owns the provisioning mechanism for the
policy-authority record consumed by the encompassing decision child; acceptance
does not provision a live record. No release context, profile set,
coverage-sufficiency value,
category-disposition mapping, policy bytes, or operator record is selected here.

### Store ownership and canonical selection input

`SemanticReleasePolicyAuthorityStore` is the sole owner of these three
private mode-`0600` stable files beneath the existing mode-`0700` workspace
directory:

- `workspaces/<repository_uuid>/semantic-release-policy-authority.json`;
- `workspaces/<repository_uuid>/semantic-release-policy-authority.previous.json`;
  and
- `workspaces/<repository_uuid>/semantic-release-policy-authority.pending.json`.

The store uses the existing descriptor-relative no-follow `DurableStateRoot`
current/previous/pending protocol. No caller chooses a path, supplies encoded
record bytes, or writes one of these files directly. Its one selection call
accepts exactly the repository UUID; expected current authority revision and
complete-record SHA-256, using revision `0` and digest JSON `null` only for
genesis; named release context; exact installed bundle-manifest SHA-256;
ordered selected-profile coordinates; complete coverage-sufficiency
declaration; canonical policy ID, version, and object; and the existing five
operator fields `action`, `issued_at`, `nonce`, `operator_id`, and `reason`.
The action is exactly `SELECT_SEMANTIC_RELEASE_POLICY`.

The store validates the installed trust-root bundle and derives every record
field and digest itself. The selected profiles must be unique, canonically
ordered, and byte-equal to manifest entries; the coverage declaration must
equal the supplied release context and profile set; the policy must bind that
declaration and its exact pair-disposition mapping. The caller chooses
those values under separate operator authority; this contract chooses none.
Unknown, additional, omitted, duplicated, incompatible, or noncanonical input,
or reordering of an order-sensitive normative array, including
selected-profile coordinates, fails before durable mutation. Equivalent JSON
object-key presentation order is canonicalized and does not change acceptance.

Every record created or advanced by this prerequisite has state exactly
`ACTIVE`. The closed record decoder continues to recognize `REVOKED` so the
decision consumer can fail closed on a future record created under separate
authority, but `SELECT_SEMANTIC_RELEASE_POLICY` does not authorize revocation,
reactivation, or any transition whose result or predecessor is `REVOKED`.
There is no `REVOKE_SEMANTIC_RELEASE_POLICY` action in this prerequisite.

The store constructs the exact format-version-1 authority body frozen below.
`authority_body_sha256` hashes the completed authority-body projection with
both `selection_authorization` and `selection_authorization_sha256` absent. The
completed selection envelope contains that body digest plus the nested five
operator fields; `selection_authorization_sha256` hashes those complete
envelope bytes and is stored only in the surrounding record. The complete
authority-record SHA-256 hashes the final record bytes and is returned by the
store but never stored inside those bytes. No digest preimage contains itself.

### Monotonic CAS and durable transaction

Genesis is admissible only when current, previous, and pending are all absent,
the expected revision is `0`, the expected digest is JSON `null`, the new
revision is `1`, and `previous_authority_sha256` is JSON `null`. Advancement is
admissible only from one stable current `ACTIVE` record whose revision and
complete-record digest equal both caller CAS values. The new revision is
exactly current plus one, and `previous_authority_sha256` is exactly the digest
of those reopened current bytes. At revision greater than `1`, the retained
previous file must be the exact revision-minus-one record named by the current
record's predecessor digest. Revision skips, rollback, a digest-only match,
same-revision different bytes, an absent or substituted predecessor, or a
`REVOKED` current record fails closed.

A stable revision `1` has previous absent. A stable revision greater than `1`
has the exact revision-minus-one previous record described above. The only
recoverable pending states are exact prefixes of the durable order below. For
pending genesis revision `1`, current and previous are absent, or current is
already byte-identical to pending and previous is absent. For pending
advancement from revision `k` to `k + 1`, the exact `ACTIVE` predecessor is
current and previous is its stable predecessor (absent when `k == 1`), current
and previous are both byte-identical copies of that predecessor after
retention, or current is byte-identical to pending and previous is the exact
predecessor. No other three-record combination is a transaction prefix.

Mutation acquires the existing exclusive registry lock and then the existing
exclusive workspace lock. While both remain held, it reopens the registered
repository UUID, stable authority state, installed bundle, complete candidate,
and CAS; validates the canonical record and predecessor chain; and revalidates
them immediately before the first durable write. No generation, lease,
pointer, journal, staged-build, semantic-queue, or decision-store lock or state
participates.

The exact durable order delegates to `DurableStateRoot.commit_record()`:

1. atomically install and sync the complete candidate bytes as pending;
2. if current exists, atomically copy and sync its exact bytes as previous;
3. atomically install and sync the same candidate bytes as current; and
4. unlink pending and sync the containing directory.

Each current, previous, or pending read and write is capped at 64 KiB. The
selection envelope remains capped at 16 KiB. The namespace has exactly three
stable record names and at most one same-directory atomic-replacement temporary
at a time, so provisioning must preflight and preserve a hard 256 KiB peak for
these record bytes. Unsafe or unexpected entries, inability to enumerate the
store-owned authority names, inadequate filesystem reserve, or inability to
prove the peak fails before pending becomes visible. This fixed provisioning
bound does not implement or satisfy `SemanticReleaseDecisionStore` capacity/GC
integration.

### Recovery, idempotency, and stop boundary

Stable reads and read-only recovery projection acquire the existing shared
registry lock and then the existing shared workspace lock. While both remain
held, they may inspect all three records but mutate nothing. Both revalidate the
applicable current/previous/pending snapshot immediately before returning;
stable read also requires pending absent plus the complete current/previous
chain. Exact recovery is authorized only while holding
the same exclusive registry-then-workspace locks and only for the original
embedded `SELECT_SEMANTIC_RELEASE_POLICY` authorization whose candidate state
is `ACTIVE` and whose revision/predecessor/body/envelope/record digests remain
valid against the installed bundle and registered repository.

Recovery may install an exact valid pending genesis only from exact genesis
absence, install an advancement only from its exact predecessor transaction
prefix, retain that predecessor as previous, adopt an already-installed
byte-identical current, and clear pending only after current and previous
revalidate as the one stable monotonic chain. It validates the original CAS
against that predecessor and never rebases it onto the candidate current. A
corrupt, `REVOKED`, foreign, stale, lower-revision, skipped-revision,
same-revision-different-byte, or otherwise divergent pending record is not
cleanup authority and remains a blocking recovery condition. Orphan atomic
temporaries may be removed only by the existing bounded `DurableStateRoot`
temporary cleanup while the same locks are held and before recovery projection;
current and previous are never deleted or rewritten as repair.

A replay of the original request and original predecessor CAS whose completed
current bytes equal the fully reconstructed candidate, whose previous chain is
exact, and whose pending path is absent returns the same revision and digest
without a write. This exact completed-outcome check precedes ordinary current
CAS rejection. Equal semantic choices with different canonical bytes or
authorization are not idempotent. A failure is an acknowledged no-commit only
when proven to precede pending visibility. Any fault once pending may be
visible is `CommitUnknown`; callers must enter exact recovery and must not
retry from assumed absence or rebase onto newer authority.

The only authorized lifecycle operations are stable read, genesis `ACTIVE`
selection, monotonic `ACTIVE`-to-`ACTIVE` advancement, read-only recovery
projection, exact transaction recovery, byte-identical replay, exact pending
clear after recovery, and bounded orphan-temporary cleanup. Revocation,
reactivation, rollback, downgrade, arbitrary repair, deletion of current or
previous, policy-authority GC, public CLI/schema/runtime-receipt exposure, ambient or
automatic policy choice, decision binding, classification composition,
omission, graph/query projection, provider/backend use, publication, and
successor activation are not authorized.

P5 and P5B2 remain `IN_PROGRESS`; the trust-root, policy-authority provisioning,
and decision-store/capacity/GC prerequisites are accepted `COMPLETE`. The
encompassing release/DLP decision, classification composition, omission, projection,
public surfaces, provider/backend, publication, remaining P5B2 work, and P5C
remain `WAITING`; H3 remains `DEFERRED`; no later successor is `READY`.

## P5B2 semantic-release decision-store and capacity/GC prerequisite

This separate internal unnumbered P5B2 prerequisite is implemented and accepted
as `COMPLETE` only at this frozen internal boundary. Completion evidence is the
[`P5B2 semantic-release decision-store and capacity/GC` receipt](receipts/p5b2-semantic-release-decision-store-capacity-gc.md),
binding PR #76's contract freeze, PR #77's implementation delivery, and PR #79's
canonical-directory correction. It owns only the private
`SemanticReleaseDecisionStore`, the decision namespace's integration into the
existing capacity and filesystem-reserve calculations, and the pre-plan
generation-eligibility blocker in the existing GC planning path. It does
not own operator
policy selection or provisioning, decision-request or full-result composition,
classification, policy reduction, a terminal release decision, omission,
redaction, graph construction, projection, query, or any public or publication
surface.

### Sole namespace and canonical binding ownership

The store is the sole owner of this private external workspace namespace:

```text
workspaces/<repository_uuid>/semantic-release-decisions/
  <generation_id>/<decision_request_sha256>.json
```

Every directory is mode `0700`. The terminal binding is one single-link regular
mode-`0600` file. Every component is opened descriptor-relatively with no-follow
semantics beneath the already-validated external workspace root. Absolute,
empty, dot/dotdot, repeated-separator, backslash, alternate, symlink, hard-link,
special-file, unsafe-mode, foreign, duplicated, or unexpected entries and any
unprovable containment or ambiguous enumeration fail closed. The namespace is
outside the sealed generation, whose admitted entries remain only `graphify-out`
and `receipt.json`. The terminal filename is derived from the complete canonical
decision-request digest; generation-only, newest-file, enumeration-selected, or
caller-selected binding identity is never authority.

First-time construction uses exactly one fixed mode-`0700`
`semantic-release-decision-publication` slot under the workspace but outside
`semantic-release-decisions` and outside lifecycle `staging`. This slot is
non-authoritative publication state, never a decision binding or lifecycle
record. It contains at most one bounded build, ready, or cleanup subtree: a
canonical manifest of at most 4 KiB plus one payload. After complete file and
directory durability, an exclusive no-overwrite rename makes the first missing
canonical root, generation directory, or binding file visible. Retry accepts
only enumerated prefixes/suffixes and removes only validated slot residue after
the encoded canonical destination is absent or byte-identical; canonical state
is never deleted, rewritten, repaired, or overwritten. Final reserve preflight
charges the candidate plus a fixed 256 KiB transient staging allowance, while
logical global/workspace ceilings charge the canonical candidate once.

The binding uses canonical
`graphify.workspace.semantic_release_decision.internal` format version 1 and the
hashed-JSON base encoding in [`state-contract.md`](state-contract.md). Its exact
top-level member set is `contract`, `format_version`, `repo_uuid`,
`target_generation_id`, `decision_request_sha256`, `promoted_entry_sha256`,
`bundle_manifest_sha256`, `policy_authority_revision`,
`policy_authority_sha256`, `semantic_input_byte_count`,
`semantic_input_sha256`, `eligible_field_inventory_sha256`, `taxonomy_sha256`,
`normalization_sha256`, `classifier_implementation_sha256`,
`classifier_abi_sha256`, `ruleset_sha256`, `selected_profile_sha256s`,
`coverage_sufficiency_sha256`, `policy_sha256`, `counts`, `field_results`,
`terminal_outcome`, and `full_result_sha256`. `selected_profile_sha256s` uses the
corresponding `utf8_lex_v1` profile-ID order from the request. `counts` contains
exactly `node_label_count`, `node_rationale_count`, `hyperedge_label_count`,
`field_result_count`, and `matched_field_count`. Each `field_results` entry
contains exactly `entity_kind`, `entity_id`, `field_name`,
`field_value_sha256`, `classifier_outcome`, `category_ids`, `rule_ids`, and
`disposition`, in the encompassing decision contract's fixed entity/ID/field
order; category and private rule IDs use `utf8_lex_v1`. Unknown, missing,
duplicated, or additional members reject the candidate.

`field_value_sha256` is SHA-256 directly over the exact captured UTF-8 field
value bytes, without JSON quoting, newline, terminator, salt, domain prefix,
renormalization, or case conversion. `full_result_sha256` is computed over a
canonical result object containing exactly `eligible_field_inventory_sha256`,
`counts`, `field_results`, and `terminal_outcome`, using the same nested shapes
and order and containing no `full_result_sha256` or `binding_sha256` member. The
binding contains `full_result_sha256` but never contains its own digest.
`binding_sha256` is computed only over the completed canonical binding bytes and
is carried externally with the request-derived identity. Raw semantic prose,
matched substrings, generated explanations, confidence scores, public source
locations, provider responses, and credentials are forbidden. The binding's
private entity/field locators and unkeyed field-value digests never become a
public or runtime receipt.

### Bounded capacity and GC protection

One canonical binding is at most 25 MiB. Bounded no-follow enumeration permits
at most 64 bindings for one generation and at most 4,096 for one workspace.
Enumeration stops at the applicable maximum plus one and closes the iterator on
every early exit. The store enumerates and proves the namespace shape, binding
counts, and byte usage before classification may begin and repeats the same
bounded proof immediately before install. The fixed binding-count limits are
independent store caps; they do not add `CapacityPolicy` fields or reuse its
generation-count fields. Decision-store bytes are charged against the existing
`CapacityPolicy` global/workspace byte ceilings and filesystem-reserve
calculation, with existing unconsumed durable byte reservations retained in that
arithmetic. The authoritative usage scanner includes this namespace.
Insufficient capacity, an exceeded limit,
unstable usage, unsafe state, or inability to prove counts, bytes, reservations,
or reserve fails closed.

A safely observed absent top-level decision namespace is the canonical
zero-binding initial state; the store may create it only at the first final
install boundary. Once the namespace or an expected request path is present,
unreadable, unsafe, or ambiguous state and any presence, entry, count, or byte
drift between the two bounded snapshots fail closed. Missing expected binding
state after possible visibility follows the commit-uncertainty rules below.

Nonempty decision state aborts the shared workspace reachability proof before a
successful GC preview or plan and therefore blocks downstream execute,
reconcile, and purge for that generation. This prerequisite adds no token to the
existing closed `GcPlan.protected` reason vocabulary and authorizes no canonical
deletion, cleanup, quarantine, repair, rollback, compaction, or GC mutation.

### Install once, replay, and commit uncertainty

The first capacity enumeration runs under the existing shared registry lock,
exclusive workspace lock, then target-generation shared lock. The caller may
classify only after that bounded snapshot succeeds, and classification runs
outside all three locks and outside this store prerequisite. Immediately before
install, the composition acquires the existing exclusive registry lock,
exclusive workspace lock, then the same target-generation shared lock. While
retaining all three, the store revalidates the registered workspace and
generation, exact request-derived path, candidate canonical bytes and digest,
namespace shape, global/workspace counts and bytes, durable reservations,
capacity ceilings, filesystem reserve, and GC eligibility state. It installs the
candidate once and reopens it to prove exact path, type, link count, mode, size,
bytes, and digest before releasing any lock. The exclusive registry lock
serializes global capacity and reserve accounting across workspaces. No new
lease, lifecycle record, journal transition, staged state, or durable
in-progress record is created.

Concurrent identical requests derive identical canonical bytes and converge on
the same install-once binding. A byte-identical completed replay is a no-write
success. A same-path different-byte candidate is a conflict, and distinct
complete requests use distinct paths without substitution authority. A failure
is definite no-commit only when proven to precede possible binding visibility.
After possible visibility, exact reopened bytes adopt the commit. Proven absence
may retry only while the exact request identity, candidate bytes, authority, and
capacity proof remain unchanged. Partial, unsafe, unreadable, different, or
ambiguous state is commit-unknown and fails closed. No failure or replay path
deletes, rewrites, quarantines, repairs, or rolls back a binding or any semantic
input, handoff, generation, receipt, journal, staged record, pointer, policy, or
other durable state.

The accepted prerequisite adds the private store, authoritative capacity
accounting, pre-plan GC blocker, internal runtime dependency, and direct hostile
and failure-injection tests. It adds no public CLI/schema/runtime receipt,
workflow, dependency, live record, JOS disposition,
provider/backend, network, publication, or release authority. P5 and P5B2 remain
`IN_PROGRESS`; the trust-root, policy-authority provisioning, and
decision-store/capacity/GC prerequisites remain `COMPLETE`; live operator policy
selection/provisioning, the encompassing release/DLP decision, classification
composition, omission, projection, remaining P5B2 work, and P5C remain
`WAITING`; H3 remains `DEFERRED`; no later successor is `READY`.

## P5B2 semantic-content release/DLP decision

This encompassing proposed unnumbered P5B2 child is contract-frozen only at the
private internal decision boundary in this section. It remains `WAITING`, not
`READY` or `COMPLETE`, and has no implementation or acceptance receipt. It
consumes the accepted trust-root, policy-authority provisioning, and
decision-store/capacity/GC prerequisites above but still depends on a provisioned
stable current `ACTIVE` operator policy-authority record, classification composition,
and every other prerequisite named below. The freeze grants no execution
authority and changes
no parent phase, accepted receipt, JOS row, or later-successor status.

### Exact promoted entry and decision request

The only admissible entry is the accepted promotion terminal for one exact
request and target. Before reading semantic prose, a future implementation must
reopen and cross-check all of the following as one proof:

1. the canonical `SyncRequest`, complete `StructuralBuildRequest`, exact staged
   format-version-1 record at `PROMOTED`, target generation, payload manifest,
   generation receipt, and recorded pointer revision/operation epoch/fence;
2. the visible pointer current bound to that same target and receipt at the
   staged pointer revision, with the matching authoritative `PROMOTED` or
   admissible `REPAIRED` journal event and no pending pointer or journal
   recovery action;
3. the verified installed target with unchanged payload inventory,
   coordination lock, retained semantic-result handoff, target-owned
   `graphify-out/semantic-inputs.json`, and immutable semantic certification
   binding;
4. durable absence of the exact promotion grant and staged-attempt digest; and
5. current registry, active-source, migration, operation, compatibility,
   state-schema, queue-policy, request, receipt, and certified-source authority
   equal to the accepted promotion proof; and
6. the exact installed semantic-release bundle manifest and current active
   operator policy-authority record described below.

No constituent record is entry authority by itself. A historical promoted
generation that is no longer exact visible current may retain a prior private
decision binding but cannot start or finish a release decision. Missing,
duplicated, foreign, substituted, incompatible, unsafe, unreadable, drifted, or
ambiguous evidence fails closed before classification.

### Classifier trust root and installed bundle

The classifier trust root owned by the separate prerequisite is a repo-owned
installed package-data file at `graphify/workspace/semantic_release_manifest.json`.
It is not caller input.
The canonical manifest binds its own format and compatibility version plus a
sorted package-relative inventory of the classifier implementation, classifier
ABI, taxonomy, normalization contract, ruleset, and every selectable coverage
profile. The manifest and its inventory use the hashed-JSON base encoding at
the top of [`state-contract.md`](state-contract.md). Every inventory entry's
common member set is exactly `artifact_kind`, `path`, `mode`, `byte_count`, and
`sha256`. `artifact_kind` is exactly `classifier`, `classifier_abi`, `taxonomy`,
`normalization`, `ruleset`, or `profile`; the entry adds exactly the matching
kind-specific ID/version pair: `classifier_id`/`classifier_version`,
`abi_id`/`abi_version`, `taxonomy_id`/`taxonomy_version`,
`normalization_id`/`normalization_version`, `ruleset_id`/`ruleset_version`, or
`profile_id`/`profile_version`. A profile entry's complete member set is
therefore exactly the five common members plus `profile_id` and
`profile_version`.

Every ID is a nonempty NFC JSON string of at most 256 UTF-8 bytes, and every
version is a positive JSON integer. `profile_id` is an ASCII identifier matching
`[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_-]*)*\.v[1-9][0-9]*`; its terminal decimal
version has no leading zero and must equal `profile_version`. Thus the required
profile coordinate is encoded as `profile_id` `"core_secrets.v1"` and
`profile_version` `1`. `mode` is exactly the JSON string `"0444"` or `"0644"`,
`byte_count` is a positive JSON integer, and `sha256` is exactly 64 lowercase
hexadecimal digits over the complete artifact bytes. Inventory paths and every
kind-specific ID/version coordinate are each unique; entries are ordered by
`path` under `utf8_lex_v1`. The manifest is loaded through installed-package
authority before any caller policy input; a missing, unsafe, oversized,
foreign, unlisted, duplicated, coordinate-mismatched, or digest-mismatched
artifact rejects release. The caller cannot add, replace, select around, or
override this inventory.

The manifest and every inventory path are resolved beneath the one canonical
installed `graphify` package root. Inventory paths are unique, strictly sorted
UTF-8 POSIX relative-normal-form paths with nonempty components; absolute paths,
empty/`.`/`..` components, repeated separators, backslashes, NUL/control bytes,
and alternate spellings are invalid. Each component and final file is opened
descriptor-relatively with no-follow semantics. Every artifact must be one
single-link regular file, have exact manifest bytes/size/digest, and have mode
`0444` or `0644`; group/other write bits and all execute bits are forbidden.
Symlinks at any component, hard links, special files, mode drift, or inability
to prove containment rejects release.

### Operator policy-authority prerequisite

Operator selection is the separate
[policy-authority provisioning prerequisite](#p5b2-semantic-release-policy-authority-provisioning-prerequisite)
owned by `SemanticReleasePolicyAuthorityStore`, not by this decision
child. Its private stable paths are:

- `workspaces/<repository_uuid>/semantic-release-policy-authority.json`;
- `workspaces/<repository_uuid>/semantic-release-policy-authority.previous.json`;
  and
- `workspaces/<repository_uuid>/semantic-release-policy-authority.pending.json`.

The stable current record is canonical
`graphify.workspace.semantic_release_policy_authority.internal`
format-version 1. It is at most 64 KiB and binds the repository UUID, named
release context, a positive monotonic authority revision, the previous
authority-record SHA-256 or canonical genesis marker, `ACTIVE` or `REVOKED`
state, installed bundle-manifest SHA-256, exact selected profile IDs and
digests, canonical policy ID/version/bytes/SHA-256, and one canonical
`graphify.workspace.semantic_release_policy_selection_authorization.internal`
format-version-1 envelope. The at-most-16-KiB envelope's top-level member set
is exactly `contract`, `format_version`, `authority_body_sha256`, and
`authorization`; the nested authorization mapping contains exactly `action`,
`issued_at`, `nonce`, `operator_id`, and `reason`. That mapping uses
the existing field validation but explicitly extends the closed internal action
vocabulary by the sole token `SELECT_SEMANTIC_RELEASE_POLICY`; it does not
change the accepted public identity action enum or schema. `authority_body_sha256`
is computed over the canonical authority-body projection, which excludes both
the entire `selection_authorization` envelope and its sibling
`selection_authorization_sha256` member. That body digest is stored inside the
envelope. `selection_authorization_sha256` is then computed over the completed
envelope bytes and stored only as the sibling member, never inside the envelope.
The complete authority-record SHA-256 is finally computed over the completed
record bytes and is not stored inside those bytes. No preimage depends on its
own digest. Reusing the nested five-field authorization with a different body
therefore cannot reproduce the selected envelope or authority-record digest.

This child only consumes a stable `ACTIVE` current record. The separate
provisioning contract may create or monotonically advance `ACTIVE` records and
recover only the original exact selection transaction. Neither contract
authorizes revocation; `REVOKED` remains recognized consumer-side fail-closed
vocabulary only. This decision child never creates, advances, repairs,
recovers, revokes, or cleans policy authority. Missing
current, present pending, divergent current/previous chaining, rollback,
unknown state or action, invalid authorization, or manifest/policy/profile
disagreement rejects release. A higher current revision supersedes every older
record and makes bindings under the older revision historical candidates only.
The provisioning mechanism is separately accepted `COMPLETE`, but no current
`ACTIVE` operator record is provisioned and this decision child remains
`WAITING`.

All release-decision objects in this section use the hashed-JSON base encoding
defined at the top of [`state-contract.md`](state-contract.md): UTF-8 JSON,
NFC-normalized strings, lexicographically sorted object keys, compact separators,
and one final newline. This contract additionally requires duplicate-free
objects and integers as the only numeric values. Arrays retain the normative
order specified below and are not reordered by JSON key sorting.
Every SHA-256 representation is exactly 64 lowercase hexadecimal digits over
the stated complete canonical bytes.

The policy authority contains one closed canonical
`graphify.workspace.semantic_release_coverage_sufficiency.internal`
format-version-1 declaration. Its top-level member set is exactly `contract`,
`format_version`, `release_context`, `selected_profiles`, and
`coverage_state`. `coverage_state` is exactly `SUFFICIENT` or `INSUFFICIENT`.
Each `selected_profiles` entry contains exactly `profile_id`,
`profile_version`, and `profile_sha256`; the array is unique and ordered by the
identifier comparator below. Each entry must equal one manifest `profile`
inventory entry's exact `profile_id`, `profile_version`, and `sha256`, with that
digest represented here as `profile_sha256`. Its entries must also equal the
authority record's selected-profile set without omission or addition, and
`release_context` must equal the authority record byte-for-byte. `SUFFICIENT`
additionally requires the exact `core_secrets.v1` coordinate and every
manifest-required profile for that named context. `INSUFFICIENT` always rejects
release. Unknown members, values, versions, missing profiles, mismatches, or an
invalid declaration are `INDETERMINATE` and reject.

`coverage_sufficiency_sha256` is the SHA-256 of those complete declaration
bytes. The policy's own canonical bytes include that exact digest, and the
policy-authority record contains both the declaration and its digest. Thus the
same `policy_sha256` cannot produce different coverage decisions.

The policy-authority record's top-level member set is exactly `contract`,
`format_version`, `repo_uuid`, `release_context`, `authority_revision`,
`previous_authority_sha256`, `state`, `bundle_manifest_sha256`,
`selected_profiles`, `coverage_sufficiency`,
`coverage_sufficiency_sha256`, `policy_id`, `policy_version`, `policy`,
`policy_sha256`, `selection_authorization`, and
`selection_authorization_sha256`. `previous_authority_sha256` is JSON `null`
only at revision 1 and otherwise the complete prior-record digest.
`selected_profiles` uses the declaration entry shape and order above. `policy`
is one canonical JSON object whose complete bytes hash to `policy_sha256`. Its
top-level member set is exactly `contract`, `format_version`, `policy_id`,
`policy_version`, `release_context`, `coverage_sufficiency_sha256`,
`pair_dispositions`, and `reduction_precedence`. Each unique
`pair_dispositions` entry contains exactly `field_type`, `category_id`, and
`disposition`, ordered by fixed field-type order `node_label`,
`node_rationale`, `hyperedge_label` and then `utf8_lex_v1` category ID.
`reduction_precedence` is exactly `REJECT_RELEASE`, `OMIT_RATIONALE`, then
`ALLOW_FIELD`. The `authority_body_sha256` projection is this complete record
with only `selection_authorization` and
`selection_authorization_sha256` removed. Unknown, missing, duplicated, or
additional members reject the authority record.

One canonical decision request, at most 64 KiB, binds all entry coordinates and
digests plus:

- installed bundle-manifest identity and SHA-256;
- current policy-authority revision and complete-record SHA-256;
- taxonomy ID, version, and manifest-bound SHA-256;
- repo-owned deterministic classifier ID, version, implementation SHA-256, and
  classifier ABI ID/version/SHA-256;
- ruleset ID, version, and manifest-bound SHA-256;
- text-normalization contract ID, version, and manifest-bound SHA-256;
- ordered selected coverage-profile IDs, versions, and manifest-bound digests;
- the complete coverage-sufficiency declaration and
  `coverage_sufficiency_sha256` equal to the current policy authority and
  policy bytes;
- operator-selected release-policy ID, version, and SHA-256 equal to the
  current policy-authority record; and
- `semantic_input_byte_count`, `semantic_input_sha256`, payload-inventory
  digest, and derived eligible-field-inventory digest.

The request's top-level member set is exactly `contract`, `format_version`,
`repo_uuid`, `target_generation_id`, `promoted_entry`, `bundle_manifest`,
`promoted_entry_sha256`, `policy_authority`, `taxonomy`, `classifier`, `ruleset`, `normalization`,
`selected_profiles`, `coverage_sufficiency`,
`coverage_sufficiency_sha256`, `policy`, `semantic_input_byte_count`,
`semantic_input_sha256`, and `eligible_field_inventory_sha256`.
`bundle_manifest` contains exactly `manifest_id` and `manifest_sha256`;
`policy_authority` contains exactly `authority_revision` and
`authority_sha256`; each taxonomy, ruleset, normalization, policy, or profile
coordinate contains exactly its type-specific `*_id`, `*_version`, and
`*_sha256`; and `classifier` contains exactly `classifier_id`,
`classifier_version`, `implementation_sha256`, `abi_id`, `abi_version`, and
`abi_sha256`.

`promoted_entry` contains exactly `sync_request_sha256`,
`structural_request_sha256`, `staged_record_sha256`, `target_generation_id`,
`payload_manifest_sha256`, `payload_inventory_sha256`,
`generation_receipt_sha256`, `pointer_revision`, `pointer_operation_epoch`,
`pointer_fence`, `promotion_journal_event_sha256`, `semantic_handoff_sha256`,
`semantic_certification_binding_sha256`, `entry_authority`,
`promotion_grant_absent`, and `staged_attempt_absent`. The last two values must
be JSON `true` and are re-proved rather than trusted during every locked
composition. `entry_authority` contains exactly `registry_revision`,
`active_source_authority_sha256`, `migration_authority_sha256`,
`operation_authority_sha256`, `compatibility_authority_sha256`,
`state_schema_authority_sha256`, `queue_policy_sha256`, and
`certified_source_authority_sha256`. Unknown, missing, duplicated, or
additional members at any level reject the request. `promoted_entry_sha256` is
SHA-256 over the complete canonical `promoted_entry` object bytes.

The request never embeds semantic-input content. Under the capture lock
composition, the implementation reopens the exact target-owned
`graphify-out/semantic-inputs.json` regular-file bytes, proves their existing
bound and canonicality, and computes `semantic_input_byte_count` plus
`semantic_input_sha256` directly over those complete bytes. It then classifies
the same captured bytes outside the locks. Any locked reread, byte-count, or
digest disagreement before install is drift and rejects release.

The complete canonical request bytes produce `decision_request_sha256`. No
provider, backend, model, credential, environment variable, network response,
live vendor catalogue, ambient default, or implicit fallback may select or
modify any classifier, taxonomy, profile, policy, or action. A caller may only
request the exact already-active authority; it may not provide policy bytes or
choose a historical binding. Unknown versions, missing declarations,
conflicting digests, authority drift, or substitution are `INDETERMINATE` and
require release rejection.

### Closed classification and coverage contracts

The separate trust-root subchild owns the deterministic classifier ABI,
closed taxonomy, ruleset, normalization, and installed profile artifacts in
this subsection. The encompassing `WAITING` decision child owns their later
composition with exact semantic fields and operator-selected policy; this
subsection does not provision or select that policy.

Version 1 is deterministic-pattern-only. It permits only locally pinned closed
patterns, exact grammars, and exact dictionaries whose canonical rules and
evaluation order are included in the ruleset digest. It permits no ML,
embedding, statistical or entropy score, opaque vendor detector, generated
inference, or contextual semantic judgment.

Installed executable evaluation reaches this module only after a private
source-executed `_graphify-semantic-authority` /
`_graphify-mcp-semantic-authority` bootstrap's POSIX shell prelude has
executed installed Python with Python environment configuration ignored,
site initialization disabled, user-site startup imports disabled, safe-path mode
enabled, and bytecode writes disabled before Python startup hooks can run; the
Python body then sets a fresh private package-external `sys.pycache_prefix`,
keeps bytecode writes disabled, and may add the installed script-prefix package
root explicitly.
Source-tree `__pycache__` entries are ignored in that runtime path. A direct
library import that bypasses this pre-import bootstrap is not semantic-release
decision authority.

The bootstrap boundary assumes the CPython executable, frozen import
machinery, standard library, wheel installer/`RECORD` verification, installed
bootstrap source, and post-install package-source integrity. It does not claim
to authenticate those already-executing prerequisites from inside the package.
If an operator cannot establish them, no installed invocation is
semantic-release decision authority even when bundle validation later succeeds.

Version 1 also uses one byte-defined classifier ABI. It scans the exact
captured UTF-8 byte sequence of each already-validated canonical field. The ABI
does not renormalize text, consult a Unicode database or Unicode category,
perform locale-sensitive comparison, or depend on host runtime text behavior.
Internal line separators are either LF or a paired CRLF; a bare carriage return
and all other frozen control ranges are invalid before matching.
Only syntax-defined ASCII names may use the ABI's explicitly specified ASCII
case fold; value bytes remain exact. Pattern grammar, dictionary encoding,
byte-comparison semantics, match-span ordering, duplicate reduction, rule
ordering, and error behavior are part of the manifest-bound ABI bytes. Runtime
or Unicode-library differences therefore cannot silently change a result; an
implementation unable to execute that exact ABI returns `INDETERMINATE`.

The closed identifier comparator is `utf8_lex_v1`: compare the unsigned bytes
of each already-canonical NFC string's UTF-8 encoding lexicographically; when
one byte string is an exact prefix, the shorter sorts first. It uses no locale,
Unicode collation table, or runtime text ordering. Selected-profile arrays,
category-ID arrays, and private rule-ID arrays use `utf8_lex_v1`. Complete
field-result arrays use fixed entity-kind order `node` then `hyperedge`, then
`utf8_lex_v1` entity ID, then fixed field-name order `label` then `rationale`.
No classifier outcome or disposition is an implicit sort key.

The classifier reports facts, not policy. Each eligible field receives exactly
one closed outcome:

- `NO_MATCH`: no category in the pinned selected profiles matched;
- `MATCH`: one or more `utf8_lex_v1`-sorted, unique stable category IDs
  matched; or
- `INDETERMINATE`: the field, classifier, ruleset, normalization, taxonomy, or
  required coverage could not be evaluated exactly.

`NO_MATCH` means only that the configured deterministic rules did not match. It
is not a DLP-clearance, compliance, confidentiality, or release-safety claim.
`MATCH` carries no confidence score, generated explanation, free-form
classification, sensitivity label, or release action. Multiple category IDs
are preserved rather than collapsed to a most-sensitive guess. Every category
ID is closed within one taxonomy version; additions, removals, semantic
redefinitions, matcher changes, and rule-ID changes require a new pinned
version and digest. Operator policy maps category IDs, never private
implementation rule IDs.

Every policy that can produce an allow outcome must select
`core_secrets.v1`. Its explicit-evidence-only categories are:

- `secret.private_key_material` for complete deterministically recognized
  private-key material;
- `secret.credential_uri` for credential-bearing URI userinfo;
- `secret.authorization_credential` for a complete supported authorization
  credential syntax;
- `secret.credential_assignment` for a closed credential key-name plus a
  non-placeholder value under an exact assignment grammar;
- `secret.provider_credential` for a complete format in the pinned local
  provider-credential pattern registry; and
- `secret.seed_or_recovery_material` only where the complete deterministic
  phrase or recovery format is defined by the ruleset.

The version-1 credential-assignment grammar is independently frozen here. A
supported assignment begins at the field start or immediately after LF/CRLF,
permits zero through 256 ASCII spaces of indentation, then one ASCII-folded key
from `api_key`, `api-key`, `password`, `passwd`, `secret`, `client_secret`,
`client-secret`, `access_token`, `access-token`, `auth_token`, `auth-token`, or
`token`. After optional horizontal separator bytes, `=` or `:` and optional
horizontal separator bytes, the value is either 1-256 bytes from
`[A-Za-z0-9._~+/@-]` or 1-256 non-newline bytes enclosed by one matching single
or double quote. The non-delimiting quote is ordinary value data; an unescaped
copy of the selected delimiter closes the value. The closing value must be
followed immediately by field end or LF/CRLF. Placeholder suppression compares
the captured value byte-for-byte with the pinned dictionary: only the exact
listed lowercase spellings are excluded, with no case fold, trim, escape, or
normalization of credential bytes. JSON-style quoted keys, comments, escape
interpretation, indentation beyond 256 spaces, and syntax outside this grammar
remain unmatched in version 1.

The version-1 labeled seed/recovery grammar likewise begins at field start or
immediately after LF/CRLF and permits zero through 256 ASCII spaces before the
ASCII-folded `seed phrase`, `seed_phrase`, `seed-phrase`, `recovery phrase`,
`recovery_phrase`, `recovery-phrase`, or `mnemonic` label. After optional
horizontal separator bytes, `=` or `:` and optional horizontal separator
bytes, the captured value is exactly 12, 15, 18, 21, or 24 lowercase ASCII
words of 3-12 letters separated by horizontal separator bytes. The complete
phrase must end at field end or LF/CRLF; indentation beyond 256 spaces and
other phrase shapes remain unmatched.

The version-1 Bearer grammar follows the line-anchored `Authorization: Bearer`
syntax and accepts one or more bytes from `[A-Za-z0-9._~+/-]` followed only by
zero or more `=` bytes. The complete captured token including padding is at most
256 bytes; padding-only, internal-padding, and overlong forms remain unmatched.

The core profile has no entropy-only rule and does not classify a bare hash,
UUID, arbitrary Base64 token, or vague secret-looking prose. It grants no
automatic exemption for samples, examples, fixtures, or test content. Syntax
that is incomplete, malformed, or outside the closed rules remains unmatched
unless evaluation itself is ambiguous; rule, parser, or normalization failure
is `INDETERMINATE`, not `NO_MATCH`.

The repo-owned taxonomy may also define pinned
`personal_data.<jurisdiction>.vN`, `financial.<jurisdiction>.vN`, other explicit
domain profiles, and exact `org.<namespace>.vN` dictionary or pattern profiles.
They are inactive unless the operator policy selects their exact IDs and
digests. A policy must explicitly declare that its selected profile set is
sufficient for the named release context. No universal PII, regulatory, or
confidentiality catalogue is inferred. Contextual categories such as legal
privilege, business confidentiality, person-name sensitivity, or free-form
health information remain outside this deterministic child until a separately
authorized authoritative metadata contract supplies them. Missing or
insufficient declared coverage rejects release.

The installed bundle and request are rejected before unbounded parsing or
classification if any frozen version-1 cap is exceeded: at most 1 MiB for the
bundle manifest; at most 25 MiB total for all referenced classifier, ABI,
taxonomy, ruleset, normalization, profile, and policy artifacts; at most 64
selected profiles; at most 4,096 taxonomy category IDs; at most 4,096 rules;
and at most 256 UTF-8 bytes for any classifier, ABI, taxonomy, ruleset,
normalization, policy, profile, category, or rule ID. These are independent
hard caps, not values supplied by or derived from the bundle. Before the host
JSON decoder materializes any document, a string/escape-aware byte preflight
also caps nesting at 64 containers, every generic array or object at 8,192
direct entries, the manifest artifact array at 8,193 entries, and aggregate
container entries at 65,552. Taxonomy, ruleset, and profile arrays use the
stricter 4,096 category/rule limits during that same preflight. Duplicate-key
rejection and exact canonical-byte comparison still run afterward.

### Exact fields, normalization, and bounds

Classification covers only the accepted post-sanitize semantic prose:

- every required node `label`;
- every present optional node `rationale`; and
- every required hyperedge `label`.

The accepted post-sanitize hyperedge schema has no rationale field. Edges,
semantic IDs, relations, confidence values, source paths, source locations, and
metadata are outside this child's content-classification surface. Their
existing integrity checks remain prerequisites and do not become DLP rules.

Each scanned value must already satisfy the accepted semantic-worker contract:
nonempty and trimmed, free of forbidden control characters, canonical
NFC-compatible protocol text, and at most 16 KiB of UTF-8. The classifier ABI
scans its exact captured UTF-8 bytes. It performs no global NFKC, whitespace
rewriting, transliteration, Unicode case folding, or locale operation. A rule
may use only the ABI-defined ASCII case-insensitive comparison for
syntax-defined names such as an authorization header or credential-assignment
key; credential values remain exact. Any field-set, encoding, normalization,
size, canonicalization, or ABI disagreement is `INDETERMINATE`.

The eligible-field inventory has exactly one entry for each field above. Each
entry carries exactly one closed `field_type`: `node_label`, `node_rationale`,
or `hyperedge_label`. Entries and field-result records use the fixed entity,
`utf8_lex_v1` ID, and field-name order above. There are at most 30,000 eligible fields: two fields for each of
the existing 10,000-node maximum plus one for each of the existing
10,000-hyperedge maximum. There is exactly one field-result record per eligible
field, with at most 256 `utf8_lex_v1`-sorted unique category IDs and 256
`utf8_lex_v1`-sorted unique private rule IDs. Exceeding any cap is `INDETERMINATE` and produces
`REJECT_RELEASE`; no environment or request may enlarge a cap.

### Policy dispositions and release outcome

Policy produces one field disposition:

- `ALLOW_FIELD`;
- `OMIT_RATIONALE`; or
- `REJECT_RELEASE`.

`INDETERMINATE` always produces `REJECT_RELEASE`. `NO_MATCH` produces
`ALLOW_FIELD` only when the operator policy's exact coverage-sufficiency
declaration is valid for the named release context; otherwise it produces
`REJECT_RELEASE`. For `MATCH`, policy maps every `(field_type, category_id)`
pair. An undefined pair, unknown category, unavailable required profile,
policy-evaluation failure, or classifier/policy drift produces
`REJECT_RELEASE`; otherwise the per-pair actions reduce to one field
disposition by the precedence below. A label may
receive only `ALLOW_FIELD` or `REJECT_RELEASE`;
`OMIT_RATIONALE` for a node or hyperedge label is invalid and rejects release.
A node rationale may receive all three dispositions. If a field has multiple
categories, `REJECT_RELEASE` takes precedence over `OMIT_RATIONALE`, which
takes precedence over `ALLOW_FIELD`.

The aggregate terminal outcome uses a separate vocabulary:

- `REJECTED` when any field requires `REJECT_RELEASE` or any fail-closed
  condition occurs;
- `ALLOW_WITH_OMISSIONS` when no rejection exists and at least one node
  rationale requires `OMIT_RATIONALE`; or
- `ALLOW_UNCHANGED` when every eligible field is allowed and the policy's
  coverage-sufficiency declaration is exact.

Deterministic redaction, replacement text, label removal, node pruning,
hyperedge pruning, ID remapping, and topology rewriting are outside this
child. A restricted label therefore rejects the whole release. A restricted
optional node rationale may be omitted without changing node identity or graph
topology. `ALLOW_WITH_OMISSIONS` is release-decision evidence for a future
separately authorized projection child; this child does not perform omission,
graph construction, or release.

### Decision-store prerequisite interface and audit evidence

The separate accepted `COMPLETE` decision-store and capacity/GC prerequisite owns the
only durable decision-evidence format: one private canonical
`graphify.workspace.semantic_release_decision.internal` format-version-1
binding installed once at
`workspaces/<repository_uuid>/semantic-release-decisions/<generation_id>/<decision_request_sha256>.json`.
This namespace is outside the sealed generation, whose admitted entries remain
only `graphify-out` and `receipt.json`. It uses descriptor-relative no-follow
access, mode `0700` directories, and one single-link regular mode `0600` file.
Unexpected names or entries, symlinks, special files, unsafe modes, duplicate
encodings, or ambiguous enumeration fail closed. The binding is
request-addressed rather than generation-only, so different authority
revisions, policies, or classifier versions cannot overwrite or masquerade as
one another.

The binding contains the exact promoted-generation coordinates and supporting
digests; installed bundle-manifest digest; current policy-authority revision
and complete-record digest; semantic-input and eligible-field-inventory
digests; taxonomy, normalization, classifier/ABI/ruleset, profile, and policy
identities and digests; scanned node-label, node-rationale, and hyperedge-label
counts; the complete ordered field-result records; aggregate terminal outcome;
and `full_result_sha256`. Each field-result record contains only the entity
kind, private entity ID, field name, exact field-value SHA-256, closed
classifier outcome, `utf8_lex_v1`-ordered category IDs and private rule IDs,
and field disposition.

The binding's top-level member set is exactly `contract`, `format_version`,
`repo_uuid`, `target_generation_id`, `decision_request_sha256`,
`promoted_entry_sha256`, `bundle_manifest_sha256`,
`policy_authority_revision`, `policy_authority_sha256`,
`semantic_input_byte_count`, `semantic_input_sha256`,
`eligible_field_inventory_sha256`, `taxonomy_sha256`,
`normalization_sha256`, `classifier_implementation_sha256`,
`classifier_abi_sha256`, `ruleset_sha256`, `selected_profile_sha256s`,
`coverage_sufficiency_sha256`, `policy_sha256`, `counts`, `field_results`,
`terminal_outcome`, and `full_result_sha256`. The selected-profile digest array
uses the corresponding `utf8_lex_v1` profile-ID order from the decision
request. `counts` contains exactly `node_label_count`, `node_rationale_count`,
`hyperedge_label_count`, `field_result_count`, and `matched_field_count`.
Each `field_results` entry contains exactly `entity_kind`, `entity_id`,
`field_name`, `field_value_sha256`, `classifier_outcome`, `category_ids`,
`rule_ids`, and `disposition`, in the canonical field-result order above.

`field_value_sha256` is SHA-256 directly over the exact captured UTF-8 field
value bytes, with no JSON quoting, newline, terminator, salt, domain prefix,
renormalization, or case conversion. `category_ids` and `rule_ids` use
`utf8_lex_v1`. Unknown, missing, duplicated, or additional binding, count, or
field-result members reject release.

`full_result_sha256` is computed over a canonical result object whose top-level
member set is exactly `eligible_field_inventory_sha256`, `counts`,
`field_results`, and `terminal_outcome`, with the exact nested shapes and order
above and with no `full_result_sha256` or `binding_sha256` member. The binding
contains that digest but never contains its own digest. `binding_sha256` is
computed only after the complete canonical
binding bytes exist and is carried by the derived terminal proof and external
binding identity. This removes any recursive or implementation-dependent
digest preimage.

The binding stores no raw semantic field value, matched substring, generated
explanation, confidence score, public source location, provider response, or
credential. Full-scan coverage is committed by the eligible-field inventory
and result digests; omission is represented by exact private entity/field
locators. Private rule IDs are audit provenance only and never policy keys.
Bounded counts are derived from and must agree with the canonical field and
field-result records.

The binding is at most 25 MiB. There are at most 64 bindings for one generation
and at most 4,096 bindings for one workspace, counted by bounded no-follow
enumeration before classification and again before install. Decision-store
bytes count against the existing `CapacityPolicy` global/workspace byte
ceilings and reserve calculation. The accepted decision-store/capacity/GC
prerequisite extends the authoritative capacity scanner to include this
namespace. Insufficient capacity or inability to prove counts and bytes rejects
release; the encompassing decision child remains `WAITING` on live policy
selection and classification composition.

No lifecycle journal transition, staged-build state, generation receipt,
runtime receipt, public schema, or public result is added by this contract
freeze. The existing promotion journal remains pointer/lifecycle authority and
does not become release-policy authority. The derived redacted internal
terminal proof contains only repository/generation and decision-request
coordinates, installed-bundle and current-policy-authority digests, result and
binding digests, bounded counts, and the terminal outcome. It never contains
private entity IDs, field names, field-value digests, category IDs, rule IDs,
raw prose, matched substrings, or public source locators. An authorized future
projection consumer must reopen the private mode-`0600` binding to recover the
exact omission set; the derived proof is deliberately not a low-entropy value
oracle.

Neither prerequisite nor decision child deletes a decision binding. The accepted
decision-store/capacity/GC prerequisite makes any nonempty decision directory a
pre-plan eligibility blocker that blocks purge of its generation; it is not
projected into the current closed public protection-reason vocabulary. Binding
deletion, quarantine, cleanup, repair, and GC mutation remain separately
unauthorized and require later operator authority coordinated with the same
generation.

### Decision-store composition, concurrency, commit uncertainty, and replay

A future encompassing-decision implementation may compose with the separate
store prerequisite only through this capture-classify-revalidate-install
sequence:

1. acquire the existing shared registry lock, existing exclusive workspace
   lock, and target-generation shared lock in that order, then reopen the exact
   entry proof, current policy authority, installed bundle, decision-store
   capacity, and bounded private semantic-input bytes;
2. capture their canonical bytes and all decision-request coordinates, then
   release coordination locks before deterministic classification;
3. classify only those captured bytes without provider, model, network, or
   ambient input;
4. acquire the existing **exclusive** registry lock, then the same exclusive
   workspace lock and target-generation shared lock, and under that order
   revalidate every entry and authority coordinate, input byte count and digest,
   global/workspace store count, capacity ceiling, and filesystem reserve; and
5. while retaining that final lock composition, install the canonical binding
   once at the request-derived path and reopen it to prove exact bytes, mode,
   size, and digest before releasing the locks.

Any revalidation drift discards the computed result and rejects release. It is
never rebased onto a newer pointer, generation, policy, taxonomy, classifier,
profile, semantic input, or field inventory. No new lease operation or durable
in-progress record is required because classification performs no durable write
before the install-once boundary.

Concurrent identical decision requests must derive identical canonical bytes
and converge on the same install-once binding. A same-path different-byte
result is a conflict. Different complete decision requests use different paths
and grant no authority to substitute one result for another. After a possible
install fault, exact existing bytes are idempotent success. Proven absence may
retry only while the entire entry proof and decision request remain exact.
Different, unreadable, unsafe, partially present, or ambiguous state is
commit-unknown and fails closed. No failure path deletes or rewrites another
binding, semantic input, handoff, generation, receipt, journal, staged record,
pointer, or policy.

A later pointer or authority change leaves an exact binding as private
historical evidence only. It removes release authority and cannot be repaired
by rewriting the binding. A replay may return a terminal decision only after a
locked reread proves the same promoted visible current, unchanged input and
authority digests, still-current `ACTIVE` policy-authority revision, exact
binding bytes, and absence of any substitution.

### Exact terminal proof and stop boundary

Terminal proof is one locked composition of:

- the complete exact promoted entry proof for the still-visible current
  generation;
- the installed bundle manifest and stable current `ACTIVE` policy-authority
  record at the exact request-bound revision and digest;
- the exact canonical decision request and request-derived binding path;
- the reopened immutable binding with equal input, taxonomy, normalization,
  classifier/ABI/ruleset, profile, policy, field-inventory, result, and binding
  digests; and
- field and match counts within the frozen bounds and an exact terminal outcome.

The consumer acquires the existing shared registry lock, exclusive workspace
lock, and target-generation shared lock in that order. While retaining all
three, it reopens every item above and revalidates all entry, request,
policy-authority, input, result, and binding coordinates. Any disagreement
rejects; no proof or allow outcome is exposed before that revalidation completes
and all three locks are released.

No absent or present artifact alone is success. `REJECTED` is a durable
decision, not content-release authority. `ALLOW_UNCHANGED` and
`ALLOW_WITH_OMISSIONS` prove only the exact policy decision at this private
boundary. They do not publish, project, merge, query, redact, omit, or expose
content, and a later consumer must independently reopen this entire proof.
That consumer must explicitly name the exact
`(generation_id, decision_request_sha256, binding_sha256)` tuple, prove the same
policy-authority revision is still the current stable `ACTIVE` record, and own
separate projection or release authority. Enumeration, newest-file selection,
historical-policy fallback, and caller-selected binding substitution are never
authority.

This freeze stops before graph construction or merge, graph/query projection,
`query_structural()` changes, execution of any omission, public semantic-sync
commands, provider/backend/model or credential selection, networking, public or
runtime schemas and receipts, status fields, migrate, repair, GC or
decision-binding cleanup, service/watch, publication, production/runtime
installation, performance/resource qualification, P5C, H3, P6+, parent completion,
implementation of the encompassing child, its readiness, acceptance, execution,
or later-successor authority.
`JOS-SEMANTIC-RATIONALE-PROJECTION` remains `OPPORTUNISTIC` with its existing
trigger unchanged. P5 and P5B2 remain `IN_PROGRESS`; the separate trust-root,
policy-authority provisioning, and decision-store/capacity/GC prerequisites are
accepted `COMPLETE`. This encompassing child, live operator policy
selection/provisioning, classification
composition, omission execution, projection, public CLI/schema/receipt,
provider/backend, publication, remaining P5B2 work, and P5C remain `WAITING`;
H3 remains `DEFERRED`; no later successor is `READY`.
