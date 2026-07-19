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
- artifact generation requires exact uv `0.11.29`, which supplies the frozen
  CycloneDX 1.5 export path;
- two fixed-epoch clean wheel builds are byte-identical;
- wheel package data contains every v1 schema;
- the schema directory, wheel, and contract bundle match one explicit frozen
  member set rather than deriving expectations from files present at runtime;
- runtime lock, skill, contract, fixtures, provenance, SBOM, rollback, and
  compatibility artifacts are SHA-256 covered by a frozen trusted manifest;
- independent wheel, skill, contract, and fixture-manifest tamper cases fail;
- two isolated clean homes resolve identical candidate dependency manifests;
- each clean-home Codex skill tree (including version and references) matches
  the bytes encoded by `skill-bundle.zip`;
- fixture-backed compensation restores binary, dependency, skill, and service
  bytes offline by declared plan order, rejects untracked mutations or executor
  order drift, publishes transaction/plan preimages, and leaves generations
  unchanged; and
- the real global Graphify binary and installed skill tree match their pre-P1
  digests afterward.

## H2 candidate packaging and security gates

Candidate packaging uses PEP 639 metadata (`license = "MIT"` plus the explicit
`LICENSE` file) and setuptools `>=83.0.0`. The redundant standalone `wheel`
development dependency is intentionally absent because setuptools has owned the
`bdist_wheel` command since 70.1. The unused Nuitka dev dependency is also absent:
its unconditional legacy `wheel.bdist_wheel` entry points polluted ordinary
setuptools builds, and no repository command or test consumes it. Candidate
builds must not emit either the deprecated TOML-table license warning or the
standalone-wheel command warning.

All release-security commands use exact uv `0.11.29`:

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
  unavailable capability decision performs no mutation;
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
  pair and exact sealed staged-input manifest binding;
- generation certification rejects caller queue/completeness mismatches and
  revalidates the captured queue revision and canonical-state hash under the
  workspace lock before sealing, including an injected queue-change race;
  queue-less new certification and same-watermark/different-payload reuse fail,
  while a durable receipt can recover after later queue advancement; and
- recursive before/after source snapshots prove queue, claim, fault-recovery,
  reconciliation, compaction, and certification operations never write the
  source checkout.

Run the focused queue suite first, then the existing workspace runtime,
generation, and freshness suites. P5A also requires the repository gates above,
exact-head CI, a current Graphify graph, and independent code, architecture, and
verification reviews.

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
- offline GC proves a no-write dry run, writes a durable intent, rechecks under
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
workspace CLI, installation, performance, candidate-publication, or live-cutover
gates, nor P6 or H3.

The P1 fixture bundle is deliberately limited to synthetic contract fixtures;
it is not claimed as the representative repository corpus required by the
shared engine-adapter verification specification. Sanitized snapshots from
Market Trend Radar, mac-mini-trading-os, and Aletheia remain required P4/P5
verification inputs before any P6-P8 migration. P1 does not read or copy bytes
from those repositories.
