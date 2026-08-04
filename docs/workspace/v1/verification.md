# Verification contract

## Exact-head GitHub review disposition gate

GitHub review evidence is repository- and revision-bound. Every GitHub CLI call
used for Graphify review or closeout must be repository-qualified. Commands
that accept `--repo` must specify `--repo Villeneuve-Ventures/graphify`;
repository-specific REST `gh api` calls must use an explicit
`repos/Villeneuve-Ventures/graphify/...` endpoint; and `gh api graphql` calls
must bind explicit `owner=Villeneuve-Ventures` and `repo=graphify` variables.
An unqualified result is not authority.

Before classifying a finding, record the canonical repository, pull request
number, source kind, and immutable source node ID when GitHub provides one. For
a review thread, also record its immutable thread node ID, current and original
path and line anchors, and explicit `isResolved` and `isOutdated` values;
`isResolved=false` records the thread as unresolved. For a PR-description or
top-level-comment source, preserve the exact claim in the durable record and
record thread identity, location, and state as `not-applicable` rather than
inventing them. Every source record also includes the full base commit, exact
reviewed head commit and tree, and an exact-SHA changed-file manifest. Invalidate
the classification if the head or governing instructions change.

Each substantive thread receives exactly one evidence-backed disposition:

- `fixed at <full-sha>` requires the final behavior plus a focused regression or
  other exact-head proof;
- `rejected: <reason>` requires a demonstrated invariant, compatibility rule,
  or reproduced evidence showing that the suggestion is not a defect; or
- `deferred: <JOS-ID>` requires a matching entry in the
  [justified out-of-scope follow-up register](governance.md#justified-out-of-scope-follow-up-register),
  including its activation trigger and closure evidence.

Resolved, unresolved, and outdated are GitHub workflow states, not technical
dispositions. An unresolved thread alone cannot create debt, and a resolved or
outdated thread cannot substitute for exact-head verification. Review
classification grants no authority to comment, reply, resolve a thread, change
ready state, merge, or clean up; every such GitHub mutation remains separately
authorized.

## P1 schema and model gates

- every positive fixture passes its normative Draft 2020-12 schema and Python
  reference model;
- every document round-trips to identical canonical bytes and SHA-256;
- unknown schema and compatibility tuples fail closed;
- repo config rejects global lifecycle overrides;
- the registry requires one explicit active source, normalized ordered remote
  aliases, UUID-enrollment evidence, and revision/source-bound rebind evidence
  stamped with distinct positive operation-epoch and fence-token acceptance;
- the sealed payload rejects links, extra fields, duplicate/unsorted paths, and
  paths outside the fixed `graphify-out` root;
- every truncated or checksum-tampered journal frame fails;
- accepted receipts, pointer records, journal events, and freshness observations
  carry distinct positive operation epochs and fence tokens;
- rollback is a post-certification journal transition with receipt and pointer
  evidence;
- pointer-prior revisions are monotonic;
- freshness cannot claim strict source linearizability or inter-observation ABA
  detection; and
- installer/rollback records require `preserve_untouched` generations, and the
  normative Python cross-document validator proves exact plan hashing, action
  coverage, root containment, ordered target-to-artifact mapping, and
  preimage-digest-checked offline-artifact linkage.

## Candidate gates

- exact baseline ancestry and candidate version are checked;
- artifact generation requires exact uv `0.11.30`, which supplies the frozen
  CycloneDX 1.5 export path;
- two fixed-epoch clean wheel builds are byte-identical;
- wheel package data contains every v1 schema;
- the schema directory, wheel, and contract bundle match one explicit frozen
  member set rather than deriving expectations from files present at runtime;
- runtime lock, skill, contract, fixtures, provenance, SBOM, rollback,
  compatibility, and canonical runtime-authority artifacts are SHA-256 covered
  by a frozen trusted manifest;
- independent wheel, skill, contract, fixture-manifest, and runtime-authority
  tamper cases fail;
- two isolated clean homes resolve identical candidate dependency manifests;
- each clean-home Codex skill tree (including version and references) matches
  the bytes encoded by `skill-bundle.zip`;
- fixture-backed compensation restores binary, runtime-authority, skill, and service
  bytes offline by declared plan order, rejects untracked mutations or executor
  order drift, publishes transaction/plan preimages, and leaves generations
  unchanged; and
- the real global Graphify binary and installed skill tree match their pre-P1
  digests afterward.

## P5C1 candidate runtime-authority gates

- `runtime-manifest.json` is the strict canonical
  `WorkspaceRuntimeAuthority` representation of the candidate's existing
  `CompatibilityManifest` plus the explicit P5C1 isolated-proof policy
  `(max_items=8, max_bytes=16384, retry_budget=1)`;
- the outer trusted manifest covers `compatibility.json` and
  `runtime-manifest.json` independently without a recursive compatibility hash,
  and two complete candidate roots remain byte-identical;
- candidate trust and the expected runtime-authority SHA-256 are checked before
  any fixture state root is created;
- `DurableStateRoot.install_once_bytes` creates exact candidate bytes at mode
  `0600`, same-byte retry preserves the inode, and a different pre-existing
  target fails closed with its bytes, mode, and inode unchanged;
- deterministic write, temporary-fsync, and replace failures occur before
  visibility and leave no new authority, while installed-hook and parent-fsync
  `CommitUnknown` cases reconcile exact candidate bytes and compensate to the
  prior absent state;
- the unchanged P5B1 loader reads the installed authority without explicit
  state writes or changes to observed mode, mtime, size, type, or file bytes;
  every failure fixture preserves generation-tree bytes, and the existing
  offline compensation proof restores a pre-existing runtime target's exact
  bytes and mode; and
- every install/failure fixture stays beneath disposable absolute `HOME`,
  `XDG_STATE_HOME`, and `CODEX_HOME` roots. The proof supplies no production
  default, publication, performance, service, provider, or global-install
  authority.

## H2 candidate packaging and security gates

Candidate packaging uses PEP 639 metadata (`license = "MIT"` plus the explicit
`LICENSE` file) and setuptools `>=83.0.0`. The redundant standalone `wheel`
development dependency is intentionally absent because setuptools has owned the
`bdist_wheel` command since 70.1. The unused Nuitka dev dependency is also absent:
its unconditional legacy `wheel.bdist_wheel` entry points polluted ordinary
setuptools builds, and no repository command or test consumes it. Candidate
builds must not emit either the deprecated TOML-table license warning or the
standalone-wheel command warning.

All release-security commands use exact uv `0.11.30`:

```sh
uv sync --all-extras --frozen
uv run --frozen bandit -r graphify tools/workspace_artifacts -lll
uv run --frozen python -m tools.workspace_artifacts build \
  --repo-root "$PWD" \
  --output-root /absolute/empty/candidate-a \
  --comparison-output-root /absolute/empty/candidate-b
uv run --frozen python -m tools.workspace_artifacts audit \
  --repo-root "$PWD" \
  --artifact-root /absolute/empty/candidate-a
```

The audit command verifies the trusted manifest, checkout commit/tree, lock
digest, wheel metadata, runtime requirements, SBOM, and a non-editable isolated
wheel installation before running `uv pip check`. It then invokes strict
`pip-audit` with `--require-hashes --no-deps --disable-pip` against the
candidate's exact `runtime-requirements.txt`, fresh locked all-extras and
dev-only exports, and marker-free hashed cohorts containing every registry
package/version record in `uv.lock`. The complete-lock cohorts prevent the host
interpreter from skipping Python- or platform-conditioned records and reject
any unauditable or non-PyPI lock source. The command never audits the editable
checkout or asks PyPI to resolve the unpublished local candidate.

The H2 refresh found and remediated every current advisory without an ignore or
baseline exception:

| Scope/package | Prior lock | Advisory disposition | H2 lock |
|---|---:|---|---:|
| optional `mcp` | 1.27.1 | CVE-2026-52870, CVE-2026-52869, and CVE-2026-59950 fixed; the published extra now floors the fully fixed release | 1.28.1 |
| optional `pillow` | 12.2.0 | PYSEC-2026-2253, -2254, -2255, -2256, -2257, -3451, -3452, and -3453 fixed | 12.3.0 |
| dev `pip` | 26.1.1 | PYSEC-2026-196 fixed; the duplicated alias record is the same underlying advisory | 26.1.2 |
| optional/dev `setuptools` | 82.0.1 | PYSEC-2026-3447 fixed and the direct build/dev floor raised | 83.0.0 |
| optional `soupsieve` | 2.8.3 | PYSEC-2026-3071 and PYSEC-2026-3072 fixed | 2.8.4 |

The three remaining high Bandit findings were non-security SHA-1 uses for
MinHash compatibility, C# namespace IDs, and path-collision salts. Each call
passes `usedforsecurity=False`; focused tests freeze the exact derived values so
this disposition cannot silently change persisted IDs or deduplication output.
The blocking CI gate reports high severity only; inherited medium findings stay
owned by H3.

## P2 runtime gates

- enrollment, adoption, evidence rotation, rebind, and activation each require
  matching operator authorization and preserve immutable UUID identity;
- UUID and Git common-directory collisions fail until shared-history and remote
  evidence support an explicit same-UUID adoption; alias rebind evidence never
  substitutes for exact active-source evidence;
- registry and workspace commits recover at every pending/previous/current
  durable-write boundary and reject corrupt or ambiguous state;
- public lease transitions keep the recovered registry snapshot locked and
  stable while nesting the workspace lock, so a higher durable pending registry
  revision cannot be skipped by current-only CAS validation;
- global registry mutations serialize across processes, and deterministic lease
  races produce one owner and one monotonic fence sequence;
- active-source activation is a revision-checked CAS; resolution never guesses
  among aliases;
- lease expiry uses monotonic time; OS-backed boot/process identity rejects
  forged reboot and PID reuse; and stale fence, owner, source, domain operation,
  and migration epochs fail acceptance;
- enrollment initializes the durable fence floor, missing initialized records
  fail closed, and workspace/semantic domains remain independently releasable
  after source, operation, or migration invalidation;
- state roots reject unsupported platforms, links, source overlap, and split
  registry/lease roots; recursive before/after source snapshots remain equal;
  and
- fault schedules cover short writes, `EINTR`, capacity/I/O errors, failed sync
  and replace, process-death boundaries, reboot identity, and commit-unknown
  recovery without fence reuse.

## Repo gates

Run the repo's five skill-generation guards, pre-commit, full pytest suite,
wheel build, CLI help, focused type checking for workspace modules, Bandit, pip-audit,
and `git diff --check`. Security findings inherited from the untouched baseline
must be separated from introduced findings.

After code changes, run the absolute repo-local Graphify binary to update the
repo graph. Because the release checkout has no committed graph, bootstrap
output remains ignored and must not widen the P1 product diff.

## P5B2a registration CLI gates

- `enroll` and `adopt` require distinct, matching operator authorization on
  standard input, an explicit canonical repo UUID, and an expected registry
  revision; no invocation may infer adoption, and registration receipt v1 does
  not admit rebind, rotation, or activation;
- parsed-operation success and failure receipts are canonical, versioned,
  deterministic, redacted, and separated between stdout and stderr with exit
  codes 0, 10, and 20 for success, conflict, and invalid state respectively;
- malformed invocations emit the exact deterministic, redacted plain-text usage
  contract on stderr with exit code 64 before authority, source, or stdin reads;
- the command reuses production authority loading and composition, requires the
  current working directory itself to be the Git top level, scrubs ambient Git
  routing overrides, rejects linked or hardlinked policy paths, and cross-checks
  the discovered UUID before mutation;
- missing, malformed, unsupported, or uncomposable runtime authority fails
  before the command reads operator authorization from standard input;
- duplicate enrollment, UUID collision, unrelated history, stale CAS,
  malformed or unsupported authority, corrupt state, unsafe paths, and
  deterministic contention fail closed without a new registry revision;
- crash failpoints recover to one durable revision, while exact external-state
  allowlists and recursive source, Git metadata, `HOME`, and `CODEX_HOME`
  snapshots prove all durable writes stay beneath the configured state root;
  and
- focused registration, runtime, composition, and CLI tests run before Ruff,
  focused Pyright and high-severity Bandit, graph refresh, the provider-neutral
  full repository suite, exact-diff review, adversarial QA, and exact-head CI.

## Identity-maintenance CLI gates

- the existing registration argv family adds exactly `rebind` and `rotate`;
  malformed or unsupported argv still emits usage on standard error with exit
  64 before authority loading, source discovery, or standard-input reads;
- installed production authority is loaded and composed before the 16 KiB-
  bounded, duplicate-free, canonically encodable authorization object is read;
  `REBIND` and `ROTATE` are exact, non-interchangeable operator actions;
- Git-top-level enforcement, bounded source discovery, exact second discovery,
  checkout verification, external-state containment, and expected registry-
  revision CAS remain the existing registration path;
- the CLI calls `RegistryStore.rebind()` and
  `RegistryStore.rotate_enrollment_evidence()` directly: shared enrollment
  history or the enrolled Git common directory is required for rebind, while
  rotation requires both an explicit binding and that same immutable enrollment
  continuity; `resolve_active_source()` independently enforces the same rule;
- rotation rejects a locator-compatible unrelated replacement before source
  evidence, identity-action evidence, or a registry revision is persisted, with
  registry, evidence, workspace, and source checkout bytes unchanged;
- neither successful operation changes `active_source`,
  `active_source_revision`, or active-source evidence; recursive source and Git
  snapshots remain byte-identical, and durable writes stay within the existing
  private registry, workspace, lock, and evidence paths;
- concurrent identical expected-revision attempts yield one success and one
  deterministic conflict that includes the safely observed revision; partial
  durable writes recover to exactly one new revision without duplication, and
  `InjectedFault` is re-raised;
- success, conflict, and invalid results are canonical, redacted, separated
  between standard output and standard error, and exit 0, 10, and 20
  respectively under the dedicated
  `graphify.workspace.identity_maintenance` CLI-v1 receipt schema; and
- the existing registration v1 schema and enroll/adopt receipts remain
  unchanged and reject rebind/rotate actions.

## Active-source activation CLI gates

- the only new argv is standalone `workspace activate` with explicit repo UUID,
  registry revision, active-source revision, operation epoch, migration epoch,
  and `--authorization-stdin`; `register activate` remains invalid;
- malformed argv exits 64 before installed-authority loading, standard-input
  reads, source discovery, or state access, while valid argv loads and composes
  installed authority before consuming authorization;
- authorization is bounded to 16 KiB, duplicate-free canonical UTF-8 JSON, has
  exactly the existing five fields, and names the exact `ACTIVATE` action;
- the existing two-pass source discovery and exact Git-checkout revalidation
  require the current Git top level and a matching explicit UUID; under the
  registry lock, the source must be explicitly bound and must share an immutable
  enrollment history root or retain the enrolled Git common-directory identity
  before mutation;
- lease owner identity, UTC and monotonic timestamps, and the bounded 30-second
  TTL are derived internally and cannot be supplied through argv or standard
  input;
- the CLI calls `RegistryStore.activate_source()` exactly once with all four
  CAS values; activation adds the durable enrollment-identity gate, rejects a
  target that is already selected before lease, evidence, or revision mutation,
  and retains its existing fencing, reservation, recovery, alias, and semantic-
  authority behavior;
- success, conflict, and invalid receipts are canonical and redacted under
  `graphify.workspace.activation` CLI v1; failures omit repo UUID and result
  epochs, and no outcome exposes authorization, paths, lease owner, or raw
  errors;
- partial durable writes recover without a duplicate registry revision,
  commit-unknown remains an invalid doctor-required outcome, and a direct
  `InjectedFault` is re-raised; and
- registration v1, identity-maintenance v1, durable workspace schemas, and the
  successful sync, query, status, and doctor contracts remain unchanged; the
  shared usage emitted for malformed register, sync, status, and doctor argv now
  includes the standalone activation form.

## P5B2 exact-last-good rollback CLI gates

- the only new argv is `workspace rollback --request-stdin`; malformed or
  extended argv exits 64 before installed-authority loading or standard-input
  reads, and root help/version checks treat it as a bounded workspace command;
- runtime authority loads and composes before one at-most-16-KiB standard-input
  read. The CLI-v1 request is canonical UTF-8 JSON, duplicate-free,
  extra-field-free, schema-valid, and complete; invalid input fails before any
  lease or pointer mutation;
- the request binds the explicit repo UUID, registry, active-source, operation,
  migration, and pointer revisions, expected current receipt, exact target
  generation and receipt, target source epoch, and the five-field canonical
  authorization with action `ROLLBACK`;
- read-only preflight verifies that the request names the visible pointer's
  exact non-null `last_good` and its target source epoch. The same target is
  revalidated after lease acquisition and before delegation; current,
  arbitrary historical, missing, corrupt, or mismatched targets fail closed;
- one `ROLLBACK` lease is acquired with trusted owner identity, UTC and
  monotonic timestamps, a fixed 30-second TTL, and only caller-supplied
  pre-acquisition registry/source/operation/migration CAS. `PointerCAS` derives
  the accepted operation epoch, migration epoch, active-source revision, fence
  token from the grant, and the current state-schema version from the frozen
  runtime constant. Post-acquisition target verification is bounded by the
  grant's liveness deadline, and monotonic time is sampled again immediately
  before pointer mutation. The exact deadline is rechecked after mutation locks
  are held and immediately before beginning the durable pointer/journal commit;
- the orchestration calls `PointerStore.rollback()` exactly once. Existing
  lease, generation, pointer, journal, GC-intent, staged-recovery, and
  commit-unknown policies remain unchanged and produce the durable
  `ROLLED_BACK` transition;
- success emits one canonical `graphify.workspace.rollback` v1 receipt on
  standard output and exits 0, binding request SHA-256, repo UUID, exact target
  generation/receipt, and resulting pointer revision. Conflict exits 10;
  invalid, unsupported, corrupt, or commit-uncertain state exits 20 with one
  redacted standard-error receipt;
- best-effort release never masks the primary failure, a release-only uncertain
  outcome is commit-unknown, and `InjectedFault` is re-raised when it is the
  primary failure or the only release failure. No receipt exposes authorization,
  source/state paths,
  lease owner, environment values, credentials, engine output, or raw
  exceptions; and
- focused tests prove authority-before-input ordering, canonical bounds,
  exact-target/no-write rejection, every stale CAS dimension, contention and
  recovery barriers, real two-generation rollback with `ROLLED_BACK` journal
  evidence, release uncertainty, broken pipes, artifact/wheel inclusion, and
  unchanged registration, activation, sync, query, status, doctor, pointer,
  recovery, and GC behavior.

## P5B2b code-only sync and staged-recovery gates

- the canonical internal staged-build record is limited to 64 KiB, uses the
  current/previous/pending durable-record protocol, and rejects corruption,
  ambiguity, links, unsafe paths, and cross-workspace identity mismatches;
- `REQUESTED` is durable before `BUILD` acquisition, the exact request and
  caller attempt digest bind every request-bound lease, same-process callers
  cannot share a live fence, and commit-unknown retry is idempotent;
- a resumed publication resets untrusted predecessor output and requires a new
  descriptor-checked inventory plus two equal typed source observations before
  `COMPLETE`;
- exact certification recovery accepts only independently durable staging or
  final receipt evidence with the required journal and semantic-certification
  bindings; a staged receipt alone is not authority;
- stale abandonment follows canonical priority across active-source, migration,
  pointer, compatibility, semantic-source-epoch, and trusted-source-observation
  drift; source unavailability alone cannot authorize it, and durable intent
  precedes destructive mutation;
- cross-workspace request, lease, state, or generation mismatch is rejected
  before source payload consumption or lifecycle-state mutation;
  selected-source discovery may read checkout and Git metadata first, and
  production typed rejections remain effective under optimized Python execution;
- failpoints, restart schedules, deterministic races, and recursive before/after
  source snapshots cover every lifecycle and no-write boundary;
- the code-only sync CLI accepts only
  `workspace sync --code-only --request-stdin`; stdin is at most 16 KiB,
  canonical, duplicate-free, schema-valid, complete, and rejected before
  orchestration when invalid;
- CLI-v1 request and receipt schemas freeze explicit identity, capacity, and
  registry/source/operation/migration/pointer authority, deterministic redacted
  stdout/stderr, and exit 0/10/20 behavior without private paths or exception
  text;
- initial and changed-source sync, exact terminal replay, every orchestration
  and ambiguous durable failpoint, lease contention, stale CAS, capacity,
  adapter, source-drift, containment, permission, link, and binding attacks
  prove build, certification, promotion, and recovery through existing fenced
  APIs only; an oversized staged payload terminally closes its impossible exact
  request and permits a corrected request with a fresh generation identity,
  while transient reservation rejection retains the exact recovery barrier;
- a build longer than its renewal interval heartbeats the same fence, renewal
  failure blocks completion and remains exactly recoverable, descriptor-pinned
  scratch resists output-ancestor replacement, and process exit cannot recreate
  removed staging through the extraction stat index;
- provider credentials and configuration cannot affect code-only sync, network
  calls are denied, exact mutation allowlists keep writes under the configured
  external state root, and recursive source, Git, real-home, Codex-home, and
  global-install snapshots remain unchanged; and
- status schema v2 and doctor render the bounded staged summary. Every
  nonterminal staged record forces `safe_to_query=false` and exposes
  `staged_build_recovery_required` / `resume_exact_workspace_sync`; terminal
  records do not block, corruption fails closed, and inspection performs no
  write or recovery.

## P5B2c one-shot certified query gates

- the only accepted public argv is `workspace query --request-stdin`; every
  other argv returns exit 64 before authority loading or standard-input reads;
- runtime authority loads and composes before standard-input consumption;
  missing, invalid, unsafe, or unsupported authority fails closed with a
  redacted CLI-v1 control record and exit 20;
- standard input is bounded to 32 KiB before decode and is duplicate-free,
  canonical UTF-8, schema-valid, complete, and extra-field-free; it carries an
  explicit repo UUID, all and only the existing `QueryRequest` fields, and an
  integer `timeout_ms` from 1 through 60000;
- malformed, unsupported-version, untrimmed, oversized, or out-of-bound
  requests reject before freshness locks or query execution. The CLI reuses
  `QueryRequest` validation rather than duplicating enforcement. The CLI-v1
  schema omits incompatible code-point `maxLength` constraints and publishes
  the frozen question, per-filter, and aggregate-filter UTF-8 byte ceilings as
  non-enforcing `x-graphify-*` annotations; exact-bound and one-byte-over
  multibyte tests keep those annotations aligned with `QueryRequest`, and any
  bound change requires a new CLI contract version;
- the composed runtime receives exactly one `freshness.query(repo_uuid,
  request, timeout_ns=...)` call and no advisory freshness status probe;
- only `decision=release` with `reason=observed_current` writes the exact raw
  native UTF-8 query output to standard output. The canonical redacted
  `graphify.workspace.query_result` v1 record on standard error binds that
  output through nested `output` metadata (`stream`, `encoding`, `bytes`, and
  `sha256`), together with the explicit repo UUID. Consumers commit captured
  output only when exit is 0, standard error contains exactly one canonical
  schema-valid release / `observed_current` record for UTF-8 standard output,
  and its byte count and digest match the captured bytes; otherwise they
  discard the uncertified output;
- all `drifted`, `timed_out`, and other `withheld` results, plus `unsupported`,
  `invalid`, and execution-failure paths, leave standard output empty and omit
  the repo UUID and `output` metadata from the control record. Exit 10 denotes
  retryable withholding; exit 20 denotes invalid or unsupported conditions.
  Existing freshness behavior collapses `LockTimeout` contention to the
  truthful `timed_out` result;
- native query logging remains bypassed, and recursive source, Git, workspace
  state, `HOME`, and `CODEX_HOME` byte/metadata snapshots remain unchanged;
  and
- focused query-CLI tests validate the frozen request/result schemas,
  canonicalization, release/withhold separation, stdout digest binding,
  redaction, and no-write behavior before the existing workspace and
  repository-wide gates.

## P5A semantic queue gates

- enqueue and exact reconciliation monotonically advance the desired watermark,
  coalesce deterministically, reject backward or conflicting revisions, and
  leave state unchanged when item or canonical-byte capacity would be exceeded;
- the persisted record round-trips canonically, recovers across every
  pending/previous/current durable-write failpoint, rejects corrupt or ambiguous
  candidates, and tolerates injected short writes, `EINTR`, `ENOSPC`, `EDQUOT`,
  and `EIO` at write, sync, and replace boundaries;
- only an explicitly available host agent or policy-allowlisted explicit
  backend may claim work, ambient environment cannot select a provider, and an
  unavailable capability decision performs no mutation; `claim` re-derives the
  decision from the registry-selected active-source policy and explicit live
  inputs, so a forged `available=True` report, foreign allowlist, or same-UUID
  relabeled policy cannot claim work; deterministic activation in the former
  registry-snapshot/workspace-lock gap makes the old claim grant stale and
  leaves the queue unchanged, while semantic-grant compaction serializes before
  activation rather than mutating under a retired source revision; every active-
  policy observation and checkout verification shares one five-second monotonic
  deadline, and every policy read is capped at 64 KiB;
- `SEMANTIC_CLAIM` owner/fence/source/operation/migration evidence is exact;
  one lease owns at most one active item, stale workers cannot checkpoint,
  complete, fail, or overwrite newer desired work, and successor recovery is
  bounded by the retry/dead-letter policy;
- deterministic round-robin operation selection and seeded replay schedules
  cover fairness, retries, stale claims, coalescing, completion, and compaction;
- queue emptiness alone cannot certify: exact source-epoch, policy, desired-set,
  watermark, and two equal typed source observations are required, with
  completed watermark equal to desired watermark; a raw pass count is not
  authority;
- compaction can remove only completed tombstones and retains the reconciliation
  and watermark proof needed by certification, including the typed observation
  pair and exact sealed staged-input manifest binding; a later same-source
  reconciliation preserves exact carried completion without duplicate claims,
  while changed identities and unfinished predecessors remain pending;
- generation certification rejects caller queue/completeness mismatches and
  revalidates the captured queue revision and canonical-state hash under the
  workspace lock before installing an immutable generation/request/queue-view/
  payload binding, including an injected queue-change race; queue-less new
  certification, a caller-preseeded staged receipt without that binding, and
  same-watermark/different-payload reuse fail; malformed requests create no
  immutable binding and corrected retries succeed, while injected binding-
  install uncertainty and later receipt/install faults recover after queue
  advancement; and
- recursive before/after source snapshots prove queue, claim, fault-recovery,
  reconciliation, compaction, and certification operations never write the
  source checkout.

Run the focused queue suite first, then the existing workspace runtime,
generation, and freshness suites. P5A also requires the repository gates above,
exact-head CI, a current Graphify graph, and independent code, architecture, and
verification reviews.

## P5B2 host-agent semantic-worker acceptance gates

These gates freeze the implemented and accepted transport boundary. The exact
delivery and validation evidence is bound by the
[P5B2 semantic-worker receipt](receipts/p5b2-semantic-worker.md); the gates do
not authorize full semantic sync or any successor:

- the public executable is `graphify`, and its only argument vector after that
  executable is `workspace semantic-worker --stdio`; any other, reordered,
  repeated, or extended argument vector exits 64 before installed-authority
  loading, standard-input reads, source discovery, lease allocation, or state
  access;
- the valid argument vector loads and composes installed runtime authority
  before consuming one canonical at-most-16-KiB `begin` frame. The single
  request/result families are version 1, reject duplicate/unknown fields and
  versions, and bind explicit registry, active-source, operation, migration,
  queue, and watermark CAS plus a 1--600000 ms absolute deadline. Observed EOF,
  catchable interruption, and malformed or oversized input before an accepted
  `begin` emit only `invalid` / `semantic_worker_request_invalid` / `none`, omit
  a begin digest, and perform no source, lease, or queue mutation;
- begin-field vectors require a canonical lowercase hyphenated RFC-variant UUID
  of version 1 through 8; positive 64-bit registry, active-source, and operation
  coordinates; and nonnegative 64-bit migration, queue, and watermark
  coordinates. They reject Booleans, strings, negative values, zero where
  positive is required, and values above 9223372036854775807 as request-invalid
  before authority comparison;
- `executor="host_agent"` and Boolean `host_agent_active=true` are required.
  Named/headless backends, provider/model/endpoint/credential fields,
  `graphify.llm` discovery or dispatch, environment inference, network calls,
  and automatic fallback are absent and denied;
- one long-lived process derives one trusted boot/PID/process-start owner,
  acquires one `SEMANTIC_CLAIM` lease, and retains that exact owner and fence
  through at most one claim, eight bounded checkpoints, one terminal `complete`
  or `fail` request, its queue transition, and release. Tests prove separate
  subprocesses cannot continue the claim;
- the current working directory is the exact active Git top level; existing
  bounded no-follow policy and checkout verification precede the redacted
  source-relative work frame. `UPSERT` stream-hashes the exact regular file and
  `DELETE` requires absence before emitting `work`, again before staging, and a
  final time after envelope/authority validation immediately before
  `complete()`. Only a completed observation proving mismatch follows
  `source_content_changed` rather than advancing the watermark.
  Open/stat/permission/read faults, including short-read faults that leave the
  observation incomplete, instead exercise retryable
  `source_unavailable` / `restore_source` without claiming content drift.
  Activation, migration, lease expiry, queue drift, and replacement desired work
  invalidate the stale session at every mutation boundary;
- `complete` accepts exactly one operation-matched payload: an `UPSERT`
  semantic fragment with exactly `nodes`, `edges`, and `hyperedges`, or the
  kind-only `DELETE` tombstone. Before the general validator or sanitizer, tests
  enforce the exact worker-specific node/edge/hyperedge field sets, types,
  enums, unique and referentially valid IDs, exact `work.path` provenance, null
  metadata fields, two through 256 pairwise-distinct members per hyperedge,
  16-KiB semantic-text limits, and rejection of duplicate members and every
  unknown nested field. Tests reject smuggling through absolute-path, raw-source,
  credential-metadata, and provider-data extension keys; only the explicitly
  bounded semantic text slots remain, and none is copied to public output;
- fixed-point canonicality vectors accept bounded scores such as `0.75` and the
  endpoint `1`, reject binary floats, exponent notation, negative zero,
  excessive precision, `1.0`, NaN, and infinity, and prove parse/serialize/hash
  parity without binary-floating-point rounding. The implemented helper
  extension preserves the existing `validate_semantic_fragment()` default for
  other callers, emits retained decimal values as unquoted canonical number tokens for
  both worker calls, and proves the sanitizer preserves every surviving numeric
  value without float or string coercion. No other request or result field
  admits a non-integer JSON number;
- sanitizer-amplification tests construct the `rationale_for` index in one edge
  pass, instrument work as `O(nodes + edges + rationale_fanout)`, and reject
  before concatenation when any projected rationale exceeds 16 KiB or the
  sanitized fragment would exceed 25 MiB. The actual sanitized copy is then
  checked against the closed post-sanitize schema and the same bounds;
- digest vectors prove `begin_request_sha256` covers the entire canonical
  `begin` frame, `work_sha256` covers only the exact canonical six-field work
  object, and `checkpoint_sha256` covers the entire canonical checkpoint frame,
  each including its final newline. Additional vectors prove `payload_bytes`
  and `payload_sha256` cover the exact whole canonical `complete.payload` wrapper
  plus its final newline for both `UPSERT` and `DELETE`, never only the nested
  fragment or the result envelope. The result-binding parser accepts only the
  exact format-version-1 object grammar, rejects missing, unknown, renamed, or
  nested fields, and requires the work/payload byte metadata to recompute
  exactly. `result_binding_bytes` and `result_binding_sha256` cover that whole
  envelope containing the payload object exactly once. Host-agent output never
  appears in a public result;
- successful output is atomically installed only at the derived private
  external path
  `workspaces/<repo_uuid>/semantic-staging/<begin_request_sha256>/result.json`
  as one canonical immutable binding. Tests cover no-follow `0700`/`0600`
  containment, same-byte idempotence, short writes, sync/replace failures, and
  reopen/rehash verification. Exact different bytes under a provably current
  claim exercise the non-retryable `semantic_result_binding_conflict=false` /
  `dead_lettered` / `inspect_semantic_queue` route; stale, unreadable, or
  ambiguous conflict state never becomes a queue failure or success;
- tight-boundary capacity vectors project the exact claim with the maximum
  `result:<64-lowercase-hex>` checkpoint before claim admission and before every
  later queue mutation. They prove a claim fitting only with `checkpoint=null`
  is withheld as `semantic_checkpoint_capacity_unavailable` without a
  current-session claim, and that concurrent enqueue, reconciliation, and
  optional checkpoints cannot consume the reserved canonical-byte headroom;
- queue completion is unreachable until the verified binding digest is stored
  as `result:<sha256>` in the current claim checkpoint, the file is reopened
  and rehashed again, that reopened digest equals the checkpoint suffix, the
  envelope's begin-request digest equals the captured digest of the accepted
  canonical begin frame, its repository UUID equals that request field, its
  claim ID, attempt, exact desired work, and work digest equal the live claim and
  captured work result, its payload object/byte count/digest equal the validated
  payload, and exact source/owner/fence/operation/migration authority still
  matches. The contained source is then reopened without following links and
  must still prove the exact `UPSERT` digest or `DELETE` absence immediately
  before `complete()`. After an observed completion return, tests release the
  exact semantic lease and prove that owner/fence absent before emitting
  `completed`. Substitution vectors vary each digest or binding independently,
  and injected races at every boundary prove no completion without the exact
  binding and no success frame before released-lease proof;
- the nine frozen failure classifications accept only their specified
  retryability. Three are accepted from `fail`; six are transport-only; and
  `semantic_work_unsupported` is also transport-derived when a work frame would
  exceed the public bound. Timeout, EOF/interruption, malformed or invalid
  result data, source unavailability or mismatch, and result-binding conflict
  attempt exactly one worker-owned `fail()` under the live claim. Failure count
  advances once; retry occurs only within the explicit budget, while
  non-retryable or exhausted work becomes durable dead-letter and blocks
  completed watermark advancement. A
  worker crash before that transition is recovered only through the existing
  `claim_expired` rule;
- deadline tests start one absolute work deadline only after the canonical
  `begin` frame is accepted and prove it bounds source verification, lease
  acquisition, protocol waits, validation, staging, checkpoint, and both the
  start and observed return of completion or caller-requested failure.
  Heartbeats use the fixed 30-second TTL and 10-second cadence without extending
  that deadline. Expiry before a claim, including during preflight or
  acquisition, emits `withheld` / `semantic_worker_preclaim_timeout` /
  `retry_status`, never calls `fail()`, and attributes no failure increment to
  the current session. Catchable preclaim interruption, checkout, configuration,
  capability, CAS, contention, capacity, and staged-barrier rejection likewise
  exercise their exact frozen terminal route without a current-session queue
  failure. Separate vectors prove `claim()` may apply the existing predecessor
  `claim_expired` transition and return no current claim; that increment belongs
  to the predecessor, and an exact recovery-only snapshot yields `idle` only
  when no item remains eligible. An uncertain `claim()` call adopts an exact
  installed claim, retries only from the exact unchanged candidate state, and
  treats every other absent-claim or unreadable state as commit-unknown. After
  expiry with a live claim, tests
  reject checkpoints, completion, and further heartbeats and permit exactly one
  transport-owned `host_agent_timeout=true` failure plus lease release before
  the unchanged lease liveness deadline;
- interruption vectors inject a catchable interruption after an accepted
  `complete` at validation, sanitization, hashing, installation, and the last
  pre-mutation boundary. They exercise exactly one
  `host_agent_interrupted=true` failure before queue completion/failure begins.
  A vector after accepted `fail` preserves its exact caller classification;
  interruption after either queue mutation begins is commit-unknown;
- registry and lease corruption vectors distinguish deterministic read failures
  before acquisition, heartbeat, or release mutation from post-mutation
  ambiguity. The former emit `registry_invalid` or `workspace_state_invalid` /
  `inspect_workspace_state` without a current-session queue failure; the latter
  follow the phase-specific commit-unknown reread rules;
- post-commit fault injection at lease acquisition adopts only the unique exact
  next owner/fence/domain-epoch record derived from the retained pre-acquisition
  snapshot; absence, contention, authority drift, and ambiguity exercise their
  distinct retry/withhold/commit-unknown paths. Heartbeat fault injection adopts
  only the exact requested timestamp and liveness deadline, permits retry only
  from the exact unchanged record, and forbids later claim mutation from an
  unproven grant;
- result-install and checkpoint commit uncertainty may be adopted only by exact
  reread. Optional progress codes reject the reserved `result:` prefix. For each
  optional or mandatory checkpoint, fault injection retains the exact prior
  claim, adopts only the same live claim with the requested value, retries only
  from the exact prior value while both deadlines remain, and maps a different
  checkpoint, absent/stale claim, or unreadable state to `commit_unknown`.
  Adopted progress emits the digest of its full canonical request frame;
  adopted result checkpoints still require envelope reopen and revalidation.
  Uncertainty after completion or failure begins, ambiguous lease mutation, or
  release-only uncertainty is `commit_unknown`, never success or replay
  authority. Release fault injection retains the exact lease, accepts an
  observed return or a locked reread proving that owner/fence absent, retries
  only from the exact unchanged record before liveness expiry, and proves no
  `completed` or post-grant `idle` frame preceded that result. It is a direct
  session result with action `none`; it invents no status route because the
  current completed queue item retains no result digest;
- every public output frame is canonical and at most 64 KiB, uses one exact
  per-kind field set, and limits failure reason/action values to the frozen
  enums. Result-schema vectors require integer `1` versions, the exact
  outcome/exit-code pairs, canonical UUID and digest strings, positive attempts
  and byte counts, nonnegative queue revisions and watermarks, and the exact
  typed six-field work object. They reject Booleans, quoted or fractional
  integers, negative values, zero in positive fields, invalid UUID/digest/work
  values, mismatched byte counts or queue snapshots, and idle watermarks with
  completed greater than desired. Values above the binary64 exact-integer range
  round-trip without narrowing. Output frames contain no source bytes, semantic
  payload, secret,
  credential, provider/model data, private absolute path, owner/fence detail,
  environment value, raw exception, or extension text. Output-writer tests prove
  short-write retry and fail closed on partial-then-error, zero progress, broken
  pipe, closed output, or flush failure. A full pipe with a non-draining reader
  exercises a near-64-KiB frame and proves readiness, writes, and flush cannot
  outlive the five-second delivery deadline; `work` and `checkpointed` use the
  earlier work deadline. Tests reject partial records, route work-deadline expiry
  through one timeout failure, route delivery-deadline expiry while work time
  remains and every other failed delivery through one interruption failure,
  forbid a replacement frame, require exit 20 for delivery failures, and reject
  a lost `completed` terminal as consumable authority;
- recursive source, Git, real `HOME`, real `XDG_STATE_HOME`, real
  `CODEX_HOME`, graph, receipt, and global-install snapshots remain unchanged.
  The only permitted writes are the reviewed queue/lease transitions and
  semantic-staging file beneath a disposable configured external state root;
  and
- no gate calls `bind_sealed_inputs()`, completes generation staging, certifies
  or promotes a generation, moves a pointer, performs migrate/GC/repair,
  retains a service/watch loop, consumes or cleans staged semantic output, or
  claims full semantic sync or governance acceptance.

## P5B2 semantic-result handoff and sealed-input finalization acceptance gates

These gates freeze the implemented and accepted unnumbered child. The exact
delivery and governance evidence is bound by the
[P5B2 semantic-result handoff receipt](receipts/p5b2-semantic-result-handoff.md).
They leave every accepted semantic-worker gate and receipt unchanged:

- no public argv, CLI schema, status field/version, runtime receipt, provider/backend
  selector, or fallback is added. The only new contract is the internal
  `graphify.workspace.semantic_result_handoff.internal` format version 1;
- entry vectors accept a result only when one canonical begin request, complete
  canonical worker-result transcript, observed process exit 0, final and only
  schema-valid `completed` terminal, and reopened immutable result-binding
  envelope agree on every overlapping begin, repository, claim, attempt, work,
  payload, and result binding. Every entry matches the outer repository, current
  active-source revision and migration epoch, and current desired source/policy
  identity. Fresh begin active-source, migration, and desired-watermark
  expectations match the captured snapshot; its original global registry
  coordinate is retained while the same repository entry is revalidated at the
  current revision. Carried evidence may retain older registry,
  worker-operation, queue, and watermark coordinates but never rewrites them.
  Original terminal queue revisions/watermarks are retained, bounded by the later
  captured queue snapshot, and the exact current reconciliation independently
  proves completion.
  `idle`, `work`, `checkpointed`, partial output, nonzero exit,
  `commit_unknown`, a cleared result checkpoint, orphan staging, manual
  inspection, or a synthesized terminal is rejected;
- carried-completion vectors accept only the byte-identical format-version-1
  worker evidence for the same `SemanticDesiredWork` identity from the verified
  semantic-input file in the exact current certified source generation selected
  by the structural request's pointer/receipt CAS. Its generation ID, receipt
  digest, semantic-input inventory entry and bytes, and payload manifest must
  agree. Arbitrary historical generations and orphan handoff scans are
  forbidden. Legacy completed queue items, prior receipts/manifests alone, or
  raw result envelopes without that record remain readable but unconsumable and
  are not silently migrated. The new result wrapper changes only hop-local
  `origin` to `carried_current_generation`; the complete begin request, session,
  result-binding envelope, byte counts, and digests remain byte-identical;
- under registry-before-workspace lock ordering, the candidate handoff binds the
  complete `StructuralBuildRequest`, its exact existing `SyncRequest` digest,
  repository, new target generation, and optional distinct carried-source
  generation identities,
  registry/active-source/operation/migration/pointer CAS, capacity and
  compatibility hashes, source commit/epoch/policy and two-equal-observation
  evidence, queue policy, revision and canonical-state hash, compaction epoch,
  desired/completed watermarks, and complete semantic-required reconciliation
  with a null sealed-input digest;
- exact-set vectors require completed watermark equal to desired watermark,
  every retained item completed, and a bijection between reconciliation desired
  work and handoff result entries. Compacted exact reconciliation remains valid;
  missing, duplicate, stale, foreign, conflicting, or extra results leave the
  state tree unchanged before staged request creation;
- canonical-record vectors require exact `target_generation_id` and nullable
  `carried_source_generation_id` top-level fields; exact per-result `origin`
  values of `fresh_worker_session` or `carried_current_generation`; the complete
  closed top-level, queue, result, session, and materialized field sets; NFC,
  sorted keys, compact separators, one final newline, retained exact unquoted
  fixed-point decimals, recomputed byte counts and SHA-256 values, and no binary
  float. The carried-source field is non-null exactly when at least one result
  is carried, all carried results name that one verified source, and source and
  target are distinct. `origin` is excluded from the immutable accepted worker
  evidence and records only this handoff hop. Unknown contracts,
  versions, fields, encoders, worker grammars, and compatibility digests fail
  closed with no migration. Reads are bounded before parse by the exact positive
  structural reservation. Capacity vectors require the shared trusted usage scan
  used by this preflight and every later allocation to enumerate all retained
  handoff files without following links. They add every exact file size to the
  matching repository/target-generation usage key, sum it with staging,
  generation, or quarantine bytes, count that target once, and count a
  handoff-only target as one generation slot. They cover multiple retained
  handoffs, exact replay without double counting, unsafe or unstable scans,
  overflow, deletion only after authorized cleanup/GC, the full new target
  reservation, both workspace/global byte and generation ceilings, and the
  filesystem reserve. Preflight failure leaves no new handoff;
- the derived handoff path is exactly
  `workspaces/<repo_uuid>/semantic-staging/handoffs/<target_generation_id>/<structural_request_sha256>.json`.
  Path tests reject caller aliases, absolute paths, escapes, symlinks, hardlinks,
  special files, wrong owners/modes, extra entries, and noncanonical generation
  or digest names. Parent directories are `0700`; the record is one regular
  single-link `0600` file;
- install-once fault schedules cover short writes, zero progress, `EINTR`,
  `ENOSPC`, `EDQUOT`, `EIO`, failed file/directory sync, failed replace, process
  death, and post-commit errors. Exact expected bytes/mode/size/digest on
  no-follow reopen adopt; exact absence retries only from the retained unchanged
  authority snapshot; different, unreadable, unsafe, or ambiguous state is
  conflict or commit-unknown and no downstream state is written. First install
  rejects any existing target staging or certified generation. After the exact
  handoff exists, replay admits only a staged record binding the same repository,
  target, and structural request in `REQUESTED`, `PUBLISHING`, or `COMPLETE`; a
  certified target or any other target state conflicts;
- deterministic materialization vectors sort by normalized path byte order,
  then ascending desired revision, operation, content digest, and result digest.
  Starting from an empty path map, exact `UPSERT` replaces a path slot and exact
  `DELETE` removes it while retaining tombstone evidence. They cover repeated
  same-path revisions, delete of an absent slot, interleaved paths, carried
  completion, operation/payload mismatch, nonascending revisions, duplicate
  work, and recomputation of the path-sorted final materialized set. No engine
  merge, entity deduplication, ID remapping, provider, or best-effort omission
  occurs;
- after the exact request-bound `BUILD` acquisition and ordinary allocation,
  every generation operation must receive the same target ID from the existing
  `SyncRequest`, and `prepare_staged_build()` must own its empty staging root. The
  structural adapter writes its existing output, and the sole semantic file is
  the byte-identical no-follow `0600` copy at
  `graphify-out/semantic-inputs.json`. Tests reject a missing, altered,
  alternate, linked, extra, or sibling semantic payload and prove recursive
  source, Git, real `HOME`, real `XDG_STATE_HOME`, and real `CODEX_HOME`
  snapshots remain unchanged;
- two fresh equal trusted observations matching both request and handoff precede
  `complete_staged_build()`. The returned sorted inventory is recomputed through
  `payload_manifest_sha256("graphify-out", entries)` and must equal the durable
  staged `COMPLETE` manifest. Capacity, payload drift, source drift, and every
  completion failpoint preserve the existing exact staged-recovery barrier;
- immediately before `bind_sealed_inputs()`, the same current `BUILD` grant
  revalidates repository, active source, operation/migration epochs, request,
  observations, the entire captured pre-bind queue revision/hash/policy/
  compaction/watermark/reconciliation snapshot, handoff, generation-owned copy,
  and staged manifest. Null-to-exact-digest is the only forward bind. Replay may
  adopt only the deterministic one-commit post-bind revision/hash containing the
  same digest; a different digest or unrelated queue advance fails closed. The
  queue is reopened and must contain that digest before grant release;
- bind fault schedules cover every queue current/previous/pending durable-write
  boundary. Exact expected post-bind reread adopts; exact unchanged pre-bind
  state may retry only under the same live grant; changed reconciliation,
  different digest, unreadable state, or ambiguity is commit-unknown. A
  watermark, handoff, or staged `COMPLETE` record alone is never success or
  replay authority;
- recovery matrices cover crashes before/after handoff install, staged request,
  allocation, `PUBLISHING`, semantic-input copy, staged `COMPLETE`, queue bind,
  and lease release. They reject source/target equality or exchange, target-ID
  mismatch at any later generation call, a different carried source, inconsistent
  null/origin combinations, and exact-path replay with different identities,
  including every handoff-install commit-unknown boundary. They distinguish the
  required empty target at first install from exact same-request recovery of
  `REQUESTED`, `PUBLISHING`, and `COMPLETE`, and reject unbound staging or a
  certified target. A successor fence resets only unsealed target-generation
  staging and recopies the retained handoff. `COMPLETE` adopts only the exact
  inventory; no path certifies, promotes, journals certification, or mutates a
  pointer;
- cleanup vectors prove an original consumed worker envelope is eligible only
  after the external handoff, generation copy, staged manifest, and queue binding
  have all been reopened and agree. The lifecycle composition alone owns that
  best-effort deletion outside the commit. The worker, queue, and generation
  store do not clean other semantic staging. The handoff is retained;
  conflicting, stale, foreign, extra, orphaned, legacy-unindexed, and
  commit-unknown staging is neither adopted nor automatically deleted and
  remains for a separately authorized semantic-staging repair or GC lifecycle.
  No fault
  removes the last recovery evidence;
- content and redaction vectors treat labels and rationales as private untrusted
  text. Public status, results, logs, and errors contain no semantic prose,
  source bytes, complete session objects, private paths, credentials,
  provider/model data, owner/fence values, environment values, or raw
  exceptions. Audit evidence is limited to identities, digests, counts,
  revisions, lifecycle boundary, and stable internal classification; and
- the terminal proof is exactly staged `COMPLETE` plus a reopened equal queue
  sealed-input digest. No gate performs content release, semantic query/graph
  projection, certification, promotion, pointer mutation, migrate, repair, GC,
  service/watch, publication, production/runtime installation authority,
  performance qualification, runtime receipt creation, parent-phase
  completion, or successor promotion. The separate governance receipt accepts
  only this bounded internal child.

Run the focused semantic-result handoff suite before repository gates. The
documentation-only acceptance closeout audits all relative links and anchors
and does not refresh the generated Graphify graph.

## P5B2 semantic-generation certification finalization contract gates

These gates freeze the next unnumbered child as a documentation-only,
`WAITING` contract. They demonstrate that existing durable state and APIs can
express the required lifecycle; they do not claim that a lifecycle composition
has been implemented, accepted, activated, or made public. Every accepted
receipt and prior acceptance gate remains unchanged:

- the documentation diff is limited to the seven canonical workspace contract
  documents. No receipt, JOS row, code, test, schema, fixture, dependency,
  configuration, workflow, or generated Graphify output changes, and P5/P5B2
  remain `IN_PROGRESS`, H3 remains `DEFERRED`, and the remaining P5B2/P5C work
  remains `WAITING`;
- entry vectors start from the accepted target generation and exact structural
  `SyncRequest`, the complete canonical `StructuralBuildRequest`, and the same
  request-bound staged record reopened in `COMPLETE`. They recompute the sorted
  payload inventory and manifest and require the retained immutable handoff and
  sole target-generation-owned `graphify-out/semantic-inputs.json` copy to
  match those bytes. They reopen the complete semantic-required reconciliation
  and require its sealed-input digest to equal that manifest;
- entry authority also revalidates repository and target identity, registry,
  active source, current operation and migration epochs, pointer CAS, capacity,
  policy, compatibility, source commit/epoch, and two equal typed source
  observations. A watermark, queue digest, handoff, staged directory,
  `COMPLETE` record, receipt, journal head, reservation, or final directory
  alone is never sufficient;
- rejection vectors cover missing, foreign, duplicated, mismatched, or
  ambiguous repository, target, request, handoff, generation copy, queue,
  source observation, inventory, manifest, sealed digest, reservation,
  binding, receipt, journal, and location evidence. Forward entry rejects
  `REQUESTED`, `PUBLISHING`, `ABANDONED`, unrelated `CERTIFIED`, `PROMOTED`, and
  inconsistent lifecycle state. A full exact terminal `CERTIFIED` proof is
  eligible only for read-only replay verification;
- lease vectors allow only `GenerationStore.acquire_staged_recovery()` for the
  same repository, target, and structural request. The grant is the
  request-bound `BUILD` recovery lane with the caller's new attempt digest and
  current operation epoch/fence. Tests reject a second staged request, a fresh
  unbound build operation, a different target, and `MIGRATE`, `PROMOTE`,
  `POINTER_RECOVERY`, repair, or abandonment authority;
- lock-order vectors retain registry-before-workspace and
  workspace-before-pre-created-generation-lock order. No generation lock wraps
  registry or workspace acquisition. Drift after grant acquisition and before
  the first certification mutation blocks the lifecycle and does not create
  cleanup or abandonment authority;
- reconstruction vectors allow existing `allocate()`,
  `prepare_staged_build()`, and `complete_staged_build()` only to recover the
  exact reservation, allocation, completion wrapper, inventory, and manifest
  already represented by staged `COMPLETE`. They prove no reset, staging
  deletion, adapter execution, payload rewrite, handoff recopy, appended entry,
  manifest change, or silent adoption of different sealed staging. A missing
  reservation is accepted only through the existing exact
  interrupted-certification recovery rule;
- certification-view vectors call existing
  `SemanticQueueStore.certification_view()` with two fresh equal source
  observations and the staged manifest. They require the exact entry repository,
  queue revision and canonical-state SHA-256, desired/completed watermark,
  compaction epoch, source epoch/commit, policy digest,
  observation-manifest/evidence digests, sealed-input manifest, and
  `semantic_completeness="complete"`. They reject `not_required`, incomplete,
  unsealed, unequal, stale, scalar-watermark-only, or differently revised
  views;
- certification-request vectors derive source commit/epoch, policy,
  observation-manifest digest, queue watermark, and complete semantic status
  from that view; use the current request-selected compatibility digest; use
  exactly the reconstructed completion entries; and require exactly the
  existing validation set `coordination_lock_precreated`, `payload_manifest`,
  and `stable_semantic_queue`. Caller-invented or predecessor-fence fields
  cannot substitute for current authority;
- `GenerationStore.certify()` ordering vectors prove that allocation, request,
  staged state, declared inventory, manifest, certification request, and any
  existing binding are prevalidated; a fresh view is recaptured and revalidated
  under the workspace lock; and the immutable target-derived binding of target,
  request digest, complete queue view, and sealed manifest is durably installed
  and reopened before any generation lock or receipt becomes authority;
- later certification vectors prove that the existing target generation lock
  protects journal recovery through `BUILT` and `VALIDATING`, exact inventory
  and sync, generation receipt install/reopen, staging-to-final movement,
  installed-generation plus binding verification, and the matching `CERTIFIED`
  journal event with pointer revision zero. Only afterward is the exact target
  reservation cleared, the generation/receipt reopened, and the same staged
  record advanced by one revision from `COMPLETE` to `CERTIFIED` with the exact
  receipt digest and certification epoch/fence;
- binding failpoint matrices cover absence, same-byte replay, different bytes,
  unreadable/unsafe state, failed write/sync/rename, and process death. Before
  binding, any queue, source, policy, pointer, epoch, request, target, handoff,
  inventory, manifest, sealed-input, compatibility, or observation drift blocks
  new certification. Proven absence retries only while all pre-binding authority
  remains exact; any other uncertainty is commit-unknown;
- receipt and lifecycle failpoint matrices cover every receipt install,
  staging-to-final move, installed-generation verification, journal append,
  reservation-clear, staged-state, and lease-release boundary. After an exact
  binding is durable, tests recover only its already-bound request and queue
  view and never adopt later drift. They reject a preseeded receipt without the
  binding, different receipt bytes, both or neither generation locations,
  mismatched journal history, a different reservation, replacement lease,
  revision jump, or ambiguous suffix;
- staged-state replay recovers only the same `COMPLETE` identity, request,
  manifest, immutable binding, and durable receipt through existing
  `GenerationStore` exact certification recovery, or adopts exact `CERTIFIED`
  with the expected receipt. Reservation-clear and lease-release uncertainty
  resolve only by a locked durable reread proving the exact state; neither
  absence alone nor a different live record is success;
- exact same-byte/state replay produces the same binding, receipt, installed
  generation, journal event, absent reservation, and staged revision. No
  failure path resets or deletes staging, abandons the target, removes the
  handoff, semantic-input copy, binding, receipt, journal evidence, or installed
  generation, rewrites or compacts the queue, or performs inferred cleanup; and
- terminal vectors require staged `CERTIFIED` for the same request, target,
  manifest and exact receipt digest; successful
  `GenerationStore.verify_generation()` for one final target with the exact
  inventory, complete semantic receipt, coordination lock, and immutable
  binding; the matching `CERTIFIED` journal event with pointer revision zero;
  durable absence of the exact target reservation; an unchanged visible
  pointer at the entry boundary; and, after release, durable absence of the
  exact recovery owner/fence. They stop there and grant no content release,
  graph/query projection, promotion, pointer mutation, public semantic-sync
  command, provider, credential, networking, migrate, repair, GC, cleanup,
  service/watch, publication, production/runtime installation, P5C, H3, P6+,
  parent completion, successor readiness, governance acceptance, or merge
  authority.

Run the existing focused state-model suite before repository gates:

```bash
uv run --frozen --all-extras pytest -q \
  tests/test_workspace_semantic_queue.py \
  tests/test_workspace_semantic_result_handoff.py \
  tests/test_workspace_staged_build_certification_recovery.py
```

Contract review must independently verify the exact committed head for both
specification consistency and architecture/lock-order correctness. The
documentation-only closeout audits all relative links and anchors and does not
refresh the generated Graphify graph.

## P5B2 public fenced pointer-repair CLI gates

- the only accepted repair argv forms are `workspace repair --dry-run
  --request-stdin` and `workspace repair --execute --request-stdin`;
  malformed, reordered, repeated, or extended argv exits 64 before authority
  loading or standard-input reads;
- the four CLI-v1 schemas freeze canonical duplicate-free UTF-8 requests and
  redacted results. Both request forms are at most 16 KiB; preview carries only
  repo UUID, registry/active-source/operation/migration CAS, and a 1--60,000 ms
  timeout, while execute additionally requires the exact preview digest and
  five-field `REPAIR_EXECUTE` authorization;
- dry-run takes only existing registry/workspace/generation locks and does not
  create a coordination object, lease, fence, recovery record, directory,
  temporary, cleanup, quarantine, or durable write. Exact recursive state-tree
  manifests (entry type, mode, size, and content digest) and a write-rejecting
  persistence seam prove this for success,
  malformed/canonical failure, stale authority, corrupt pointer, corrupt
  journal, corrupt generation, and lock-contention paths;
- canonical preview output is deterministic and bounded: it classifies only
  `no_op`, `repairable`, or `irreparable`, and contains only observed authority,
  request digest, candidate/last-good references, selected source, prospective
  revision/action, projected journal actions, the exact-decision digest, and
  sorted quarantine IDs. Tests
  reject paths, authorization, owner/fence, raw durable records, environment
  values, and raw errors in all public results;
- execute recomputes the exact preview result and compares the SHA-256 of its
  canonical bytes including the final newline before it acquires fresh
  CAS-bound `REPAIR` authority. Under the required locks it recomputes the
  private exact plan and rejects every preview/authority/plan mismatch before
  journal recovery, temporary cleanup, pointer mutation, or quarantine;
- `PointerStore` remains the mutation authority. Focused failpoint tests cover
  every pointer/journal boundary, a stale preview after lease-epoch advance,
  wrong authorization, candidate substitution, corrupt excluded-generation
  quarantine only, fenced no-op behavior, one absolute deadline shared by
  preview and execution, preserved unsafe-path/timeout/commit-unknown failure
  classifications, and commit-unknown handling. A commit-unknown caller
  must inspect status and create a fresh preview/request pair; exact old execute
  requests cannot apply a second repair;
- valid GC intent routes to `run_workspace_gc_reconcile`; nonterminal/corrupt
  staged state, semantic-queue corruption, registry/lease faults, and unsafe
  paths route to their own stable inspection/resume/configuration action rather
  than pointer repair; and
- status and doctor retain existing-only read behavior. Doctor never invokes a
  repair form, and no test or verification claim treats this implementation
  delivery as governance or receipt acceptance.

## P5B2 public offline-GC CLI gates

- the only accepted preview argv is
  `workspace gc --dry-run --request-stdin`; malformed, reordered, repeated, or
  extended argv exits 64 before authority loading or standard-input reads;
- installed runtime authority loads and composes before one bounded canonical
  request is read. The CLI-v1 request and result schemas freeze every field;
  the request requires the explicit repo UUID, all five expected revisions,
  timeout, complete `CapacityPolicy`, and all six `GcProtection` classes;
- the runtime receives only those explicit values, performs read-only
  registry/workspace coordination and generation-lock probes, and accepts a
  preview only after two matching reachability snapshots. It creates no
  `LeaseGrant`, fence, or executable `GcPlan`;
- success emits one deterministic unfenced canonical preview result with the
  candidates, protected generations and stable reasons, observed revisions, and
  capacity-policy SHA-256. Stable conflict, withholding, invalid, unsupported,
  corrupt, recovery, and contention failures are redacted and use the frozen
  10/20 exit interpretation;
- command-level recursive bytes/metadata snapshots plus a write-rejecting
  durable-state syscall seam prove zero writes across success and concrete
  failure families over source, Git, external state, `HOME`, `XDG_STATE_HOME`,
  and `CODEX_HOME`, including no lease, recovery, temporary cleanup, directory,
  mode, lock-file, registry, intent, quarantine, receipt, or query-log mutation;
  and
- focused tests cover request/result schemas, canonicalization and bounds,
  redaction, determinism, explicit-capacity/protection rejection, stale CAS,
  observation instability, coordination contention, and unchanged fenced P3
  `GcStore.plan()`, `execute()`, `reconcile()`, and `purge()` behavior.

- `gc --execute --request-stdin`, `gc --reconcile --request-stdin`, and
  `gc --purge --request-stdin` accept no alternative or reordered argv and
  require their respective `GC_EXECUTE`, `GC_RECONCILE`, and `GC_PURGE`
  authorizations after one bounded standard-input request is read and parsed,
  before any lease acquisition or mutation;
- execute recomputes the frozen canonical preview result and verifies the
  request's SHA-256 against those exact bytes before acquiring a fresh trusted
  `GC` lease. A fresh plan must match the approved preview on repo UUID,
  registry/active-source/migration/pointer revisions, capacity-policy digest,
  candidates, and semantically equivalent protected facts, while excluding the
  newly allocated fence and operation epoch. `shared_lock` is ignored only when
  another reason already protects the same generation; a sole lock reason
  remains material. The one request deadline continues through plan validation,
  blocking generation-lock acquisition, and fenced store mutation;
- reconcile mutates only an existing intent. A completion indexed by the
  request's still-current operation epoch replays without a lease or write;
  otherwise no recovery state returns `nothing_to_reconcile`. Purge is an
  explicit idempotent completed-plan operation selected by
  `expected_plan_sha256`. Exact terminal replay returns its durable no-write
  result before mutation admission; a first-time deletion performs pointer,
  protection, and lock rechecks;
- phase success and failure result schemas prove canonical redacted public
  output and the 0/10/20 exit mapping. Receipts omit authorization, raw durable
  lifecycle documents, fence/owner data, paths, timestamps, operation epochs,
  environment values, and raw errors;
- valid unresolved GC intent status and doctor output directs
  `run_workspace_gc_reconcile`, without an automatic reconcile path; and
- generation enumeration proves descriptor-relative no-follow validation and a
  hard 4096-plus-one bound that raises before overflow is materialized. This is
  a safety assertion, not performance or resource certification.

## P3 runtime gates

- capacity limits and the filesystem reserve fail before allocation mutation,
  with registry-serialized durable reservations for concurrent workspaces and
  a bounded two-observation filesystem scan that tolerates concurrent renames
  but rejects persistent duplicate locations;
- filesystem reserve preflight counts unconsumed bytes promised by every
  durable reservation before admitting another cross-workspace allocation;
- successor fences adopt only byte-, policy-, source-, and generation-identical
  reservations, forged allocation objects fail against durable state, and
  activation is blocked while a reservation remains outstanding; a successor
  revalidates before sealing a new receipt, may finish a fully sealed
  predecessor receipt after binding it to the predecessor's validating event,
  and idempotently returns an already-certified result after durable capacity
  release;
- descriptor-relative payload inventories reject links, special files,
  hardlinks, extras, unstable identities, invalid root or descendant modes,
  and noncanonical declarations;
- certification syncs the exact payload and receipt, atomically installs one
  generation, reopens and verifies it, then appends owner-bound `CERTIFIED`;
- journal recovery adopts one complete uncommitted hash-linked segment,
  discards only one truncated tail, cleans validated private temporary files
  left by real process death, recomputes repo-bound event IDs, and rejects
  cross-workspace grafts, committed corruption, or an ambiguous suffix;
- promotion retains the prior pointer before one visible replacement, stale
  candidates become `SUPERSEDED`, corrupt pending state fails closed, pointer
  documents remain workspace-bound, and recovery emits and immediately verifies
  a fresh higher revision without quarantining a valid retained generation;
  stale pending/current revisions and missing certification history fail closed,
  while every interrupted repair boundary resumes without exposing partially
  verified references;
- shared readers open retained locks read-only, perform no durable write, and
  exclude GC's exclusive counterpart;
- fenced offline GC proves a no-write dry run, writes a durable intent, rechecks under
  lexical generation locks, quarantines with both directories synced, records
  completion, reconciles `commit_unknown`, and purges only by a separate
  explicit operation;
- durable GC and pointer intents block conflicting operations before mutation,
  copied cross-workspace recovery records fail closed, and current readers stay
  safe at every GC phase; purge retries injected unlink, rmdir, interruption,
  and parent-fsync failures before recording completion; and
- focused failpoint and deterministic concurrency schedules cover segment,
  generation, pointer, reader-lock, GC, clean-reboot, dead-builder adoption, and
  cross-workspace lock-scope boundaries.

## P4 adapter and freshness gates

- all Graphify-private engine imports are confined to the one versioned
  `0.9.16` adapter package;
- exact tuple selection executes `0.9.16`, mixed tuples reject before later
  state use, and coherent future tuples remain probe-only and non-promoting;
- no pre-workspace retained-state reader or import intent is exposed, and only
  the exact supported tuple can stage or promote a newly built generation;
- read-only detection suppresses stat/word-count persistence and conversion
  sidecars, while explicit ordinary output roots redirect sidecars outside the
  source checkout;
- two consecutive complete descriptor-checked inventory passes form each side
  of the query; detector probes, source entries, policies, and query payloads
  share rooted no-follow reads, with source identity and pointer/receipt
  revalidated at the release boundary;
- linked worktrees pin the checkout, per-worktree Git directory, and shared Git
  common directory independently; only `.git`, `commondir`, and
  `info/exclude` routing/policy reads can cross the checkout boundary;
- structural extraction consumes only a descriptor-validated external source
  snapshot in a private build directory, publishes through a pinned empty output
  descriptor while retaining source authority through publication, revalidates
  the digest of every selected source and effective policy input plus
  source/output bindings and exact destination contents around the copy, rejects
  per-file extractor errors, preserves reciprocal directed edges, normalizes
  staged payload modes independently of caller umask, and deterministic contract
  bundles exclude generated adapter cache trees;
- query mode, depth, and token-budget types reject before comparison; query text,
  term work, context filters, depth, and token budgets reject above their
  workspace bounds before freshness locks are acquired; registry and generation
  lock contention plus subsequent registry, pointer, receipt, journal, and
  release-revalidation phases are bounded by the same deadline and perform zero
  source or workspace writes;
- deterministic schedules cover edit, create, delete, rename, replacement,
  source, classifier, policy, query-payload, and output ancestor-to-symlink
  replacement, plus a real-directory detection/snapshot replacement, with zero
  external access, policy change,
  post-pass identity change, persistent churn, pointer change, wholly
  inter-observation ABA, and post-boundary mutation;
- native query bypasses optional query logging, and recursive bytes,
  write-sensitive metadata, xattrs, read-only source modes, and filesystem-event
  observation prove no source or workspace write; and
- focused adapter/freshness/compatibility tests run before the full repository,
  security, packaging, graph-refresh, exact-head CI, and independent-review
  gates.

P5A verification satisfies only the durable semantic queue and stable-watermark
certification slice. It does not satisfy the remaining P5 watch, service,
workspace CLI commands beyond registration, installation, performance,
candidate-publication, or live-cutover gates, nor P6 or H3.

The P1 fixture bundle is deliberately limited to synthetic contract fixtures;
it is not claimed as the representative repository corpus required by the
shared engine-adapter verification specification. Sanitized snapshots from
Market Trend Radar, mac-mini-trading-os, and Aletheia remain required P4/P5
verification inputs before any P6-P8 migration. P1 does not read or copy bytes
from those repositories.
