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

The public executable is `graphify`. Its only future argument vector after that
executable is exactly `workspace semantic-worker --stdio`, producing this full
invocation:

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

The future implementation must extend `validate_semantic_fragment()` with a
lossless canonical-number encoder hook while preserving its current default for
all existing callers. Both worker calls to that helper use the hook to encode
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

Source checkout bytes and modes, Git metadata, and real `HOME`,
`XDG_STATE_HOME`, and `CODEX_HOME` remain unchanged in verification. A future
implementation may write only the reviewed external workspace state described
above, under disposable configured roots in tests. No receipt, governance
acceptance, or implementation evidence exists for this contract-only child.
