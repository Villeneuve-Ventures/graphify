# Verification contract

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
