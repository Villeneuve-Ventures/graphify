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

## P3 runtime gates

- capacity limits and the filesystem reserve fail before allocation mutation,
  with registry-serialized durable reservations for concurrent workspaces;
- descriptor-relative payload inventories reject links, special files,
  hardlinks, extras, unstable identities, and noncanonical declarations;
- certification syncs the exact payload and receipt, atomically installs one
  generation, reopens and verifies it, then appends `CERTIFIED`;
- journal recovery adopts one complete uncommitted hash-linked segment,
  discards only one truncated tail, and rejects committed corruption or an
  ambiguous suffix;
- promotion retains the prior pointer before one visible replacement, stale
  candidates become `SUPERSEDED`, and recovery emits a fresh higher revision;
- shared readers open retained locks read-only, perform no durable write, and
  exclude GC's exclusive counterpart;
- offline GC proves a no-write dry run, writes a durable intent, rechecks under
  lexical generation locks, quarantines with both directories synced, records
  completion, reconciles `commit_unknown`, and purges only by a separate
  explicit operation; and
- focused failpoint and deterministic concurrency schedules cover segment,
  generation, pointer, reader-lock, and GC boundaries.

P3 verification does not satisfy P4/P5 adapter, freshness, queue, service,
performance, installation, or live-cutover gates.

The P1 fixture bundle is deliberately limited to synthetic contract fixtures;
it is not claimed as the representative repository corpus required by the
shared engine-adapter verification specification. Sanitized snapshots from
Market Trend Radar, mac-mini-trading-os, and Aletheia remain required P4/P5
verification inputs before any P6-P8 migration. P1 does not read or copy bytes
from those repositories.
