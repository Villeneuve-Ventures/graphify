# Merge lifecycle contract for #105, #102, and #104

Status: selected bounded contract, 2026-10-08. Delivery B source baseline: v8
`3cecae9bed3f9cb02caa70788fae8faa583f8828`. The earlier Delivery A observations
below refer to `0373e6d74675891c83f4cf7bbe5c5e5d5311ed4a` where stated.
This record separates the bounded #105 containment delivery from later
successful finalization. It does not establish runtime acceptance, authorize
successor implementation, or change the scope of #102 or #104.

## Evidence and ownership

One design owner owns the authority and integration contract. Independent
reviews advise that owner; they do not establish acceptance or expand scope.

Current source has post-event recovery, a receipt-bound artifact inventory,
and directory-identity checks on managed reads. These are source observations.
Historical automatic/manual merge and fresh-clone failures at `a558766` are
revision-specific runtime evidence. On 2026-10-08, the four tests in
`tests/test_merge_commit_lifecycle.py` passed on the baseline above with Git
2.55.0 and Python 3.14.3 on macOS, before hook implementation changed. The
automatic merge recorded the hook-entry tree despite a changed exit index;
manual pre-commit staging reached its commit. Both Graphify merge paths recorded
`merge_pending`, later recovered an active but dirty working graph, and failed
fresh-clone reading without dirtying the clone. Those fixtures track graph.json
only; they do not prove complete-inventory portability. No old candidate patch
is an implementation source.

The baseline proof identifies the tree consumed by each tested Git commit path.
Staging from `pre-merge-commit` did not replace the prepared automatic-merge tree.
This is why the first delivery refuses a pending candidate at hook entry rather
than attempting automatic finalization.

## Delivery A: narrow containment

Containment prevents an affected ordinary merge from recording a graph with
the exact managed `merge_pending` state. It does not finalize that graph and
does not close #105.

- At automatic-merge hook entry, capture the relevant candidate index state
  before invoking a user hook. A user hook must not conceal a pending candidate
  that Git may already have selected for the commit. This capture grants no
  staging or mutation authority.
  The installed standalone guard freezes the selected mode and object ID before
  interpreter discovery. Its guarantee starts at that observation: Git does not
  supply its private selected tree to the hook, so concurrent index changes
  before capture remain unsupported. This is not isolation from other index writers.
- At manual/continued merge commit, inspect the effective candidate index.
  Support the ordinary two-parent merge case first. Detect unsupported cases
  without claiming to cover squash, octopus, rebase, or cherry-pick delivery.
- Refuse an affected pending candidate without Graphify output or index writes.
  Preserve user-hook effects; do not roll them back. Explain the pending state,
  supported continuation limits, and explicit bypass behavior.
- Do not add a general incomplete-closure gate that blocks active v1 outputs,
  ordinary non-merge commits, or legacy outputs merely because portable reading
  has not been implemented. No broad artifact enrollment occurs in this slice.
- Preserve user-hook execution, arguments, environment, interpreter, and failure
  status through supported composition. Missing required Graphify runtime must
  not silently turn an affected guard into success. Installation, status, and
  removal must agree, including `core.hooksPath` and linked worktrees.
- Honor documented hook bypasses. A bypassed pending commit remains unreadable;
  neither recovery nor a post-event hook may amend it or create a hidden commit.

Acceptance proves both refusal and preservation, and includes unaffected
commits, active v1 output, user-hook failure, hook nesting, bypass, and missing
runtime. Existing installer recovery guarantees must not be overstated as
whole-installation atomicity.

The initial implementation admits only standalone Graphify prehooks. Existing
user prehooks and foreign content are refused without replacing them; user-hook
composition and nesting therefore have no supported execution path in this
slice. Output enrollment requires a repository-relative literal selector that
the current merge driver can express. Absolute selectors and whitespace or
attribute-pattern characters are refused before hook publication.

### Local containment verification, 2026-10-08

On macOS with Git 2.55.0 and Python 3.14.3:

- The combined lifecycle, guard, installer-recovery, promisor-object, and README
  policy run passed 202 tests. After adding the three noncanonical-selector
  regressions and their repair, all 48 guard tests passed. Other test inputs were
  unchanged.
- Existing hook and hook-installation tests passed 367 cases with seven skips
  across the initial run and the focused retry after restoring default-call
  compatibility. The initial run took 124 seconds; its 22 failures were caused
  by the added keyword reaching existing default-call test doubles.
- Replacement-object and inherited-pathspec bypasses were reproduced before
  repair. A real missing-promisor-object fixture also reproduced fetch attempts;
  after repair it refused without invoking the transport or changing repository
  file bytes, modes, or inodes.
- Ruff and the changed-source Bandit check at the repository's medium/high
  threshold passed. Pyright reported five optional-value errors in
  `hook_installation.py`; checking the unchanged baseline file reproduced the
  same five errors. Type checking is therefore not clean.
- Independent source re-review found no remaining concrete containment blockers.
  This is local evidence, not hosted CI, a full-suite result, or Delivery B
  acceptance. This verification was completed before commit and PR publication.

## Delivery B: portable reading, then manual finalization

The smallest successful slice is an explicitly enrolled, repository-relative
output with an ordinary two-parent manual/continued merge, a supported effective
index, and unchanged authenticated predecessor sibling inventory. It produces
a complete readable committed bundle. Automatic successful finalization is a
separate extension after Git tree-consumption proof.

Portable read semantics are a prerequisite for this slice's fresh-clone claim.
The selected contract uses a small versioned portable envelope, reusing bounded
inventory validation, while retaining the existing local capability protocol.
Delivery B is split into B1 portable reading and B2 manual transition publication.
Neither slice establishes recurring manual merge lifecycle completion.

### B1: selected portable reader profile

The envelope is `.graphify_portable.json` inside one literal repository-relative
output. Its format is `graphify.portable`, version 1, with minimum reader contract
1. The exact committed closure is this envelope, `graph.json`, and `manifest.json`;
all three are regular files with Git mode `100644`. Unsupported output siblings
refuse. This profile does not enroll the default report/label/HTML output set.

The envelope declares the output selector, graph and manifest formats, source
selector, extraction contract, source-projection digest, artifact paths, sizes,
modes and SHA-256 digests, and content identifier. The graph retains the managed
`_graphify_protocol` key with schema 2, epoch 2, and state `portable_complete`.
This marker describes portable data, not an upgrade of local transaction authority.
No local generation, token, receipt, drainer, or physical directory identity is
needed to read it.

Envelope and source-manifest JSON is canonical UTF-8 without BOM or final newline,
with sorted keys and compact separators. Duplicate keys, unknown schema fields,
non-finite numbers, lone surrogates, invalid field types, and noncanonical metadata
bytes refuse. Metadata fields use bounded integers, not floats. Graph attributes
may contain finite floats. Paths are canonical relative POSIX paths; aliases,
traversal, case/Unicode-normalization collisions and unsupported path forms refuse.
Every tracked path, including non-Python files, must avoid aliases of the output
directory and its parents. The complete serialized Git tree inventory is limited
to 8 MiB; preparation includes the prospective three-file closure in that check.
Each metadata artifact is limited to 8 MiB, and the combined portable closure to
256 MiB. The producer's bundle validation checks all object sizes before reading
any artifact. Inventories are
sorted by path. Content identity is SHA-256 of `graphify.portable.v1` followed by
a NUL byte and the canonical envelope without `content_id`. The envelope excludes
itself from its artifact list; the graph does not embed this content identifier.
The containing commit/tree identifier is returned as read evidence, not included
in hashed metadata, which avoids self-reference.

Query-facing node identifiers and optional labels, normalized labels and source
paths must be strings. Optional edge context must be a string; multigraph keys,
when present, must be strings or integers. Serialized query caches and learning
overlays refuse: they are runtime state, not portable graph metadata.

The first source selector is `tracked-python-v1`: all tracked regular `.py`
files outside the output namespace, from one frozen Git tree. Python source modes
`100644` and `100755` are supported. Submodules and nonregular selected sources
refuse. Ignore files, environment configuration, untracked files, working-tree
changes, timestamps, and scratch-directory names are not extraction inputs.
This is an explicit Python AST profile, not full-repository or multilingual
extraction. The new portable source manifest enumerates exact path/mode/SHA-256
records. Readers independently recompute the complete selected projection; they
do not merely check the rows supplied by the manifest. The projection digest
hashes the canonical source manifest. The local incremental manifest format is
unchanged and is not reinterpreted as portable evidence.

The bundle preserves distinct raw relation records and declares missing extractor
endpoints as reference nodes. The query command uses the existing simple graph
projection: multiple relations with the same endpoints collapse in that view.
Raw-record preservation does not promise that each relation appears in a query.

`open_portable_graph_snapshot` returns a separate immutable read-result type.
`graphify query "question" --portable --output graphify-out --revision HEAD`
validates working-copy artifact bytes against the selected committed tree and
queries retained bytes without query logging. Local/legacy reads never fall back
to portable mode. Portable support for `path`, `explain`, `affected`, MCP/serve,
exports, incremental builds, recovery and local adoption is outside B1.

Envelope presence reserves the namespace even if the envelope is malformed or
orphaned. New local readers and writers refuse it before mutation. Portable
admission requires local coordination to be absent, including malformed, pending,
or apparently complete coordination. An envelope is a portable authority signal,
not itself local coordination. This initial refusal rule does not prohibit a
future separately versioned local binding that preserves every portable byte.

Minimum reader contract compatibility is separate from release/build provenance.
The existing package version `0.10.0` alone does not establish portable support.
Enrollment requires a build qualified for reader contract 1; compatible successor
builds may also read it. Old-reader qualification covers correctly emitted bundles
through enumerated protocol-aware Graphify consumers. Generic JSON readers and
corrupted bundles whose managed marker was removed are not covered by that old
reader promise. New readers must refuse those partial portable states.

### B2: manual transition publication

Registration is declaration of output, profile and reader requirement; it is not
publication or local write authority. The explicit `graphify merge-finalize
--output graphify-out` request selects this transition. It does not require a
portable bundle to have been committed first, which would invalidate v1-origin
admission. This command stages a complete bundle; the operator's subsequent
ordinary manual merge commit records it.

The first admitted predecessors are authenticated v1 graphs with unchanged local
inventory containing graph and manifest, optionally the authenticated local
`.graphify_root` marker. That marker stays unchanged locally and is not a portable
payload or extraction input. A tracked root marker or any other tracked output
sibling refuses; no broad removal or silent conversion occurs. Other local
managed siblings also refuse. This profile is suitable for a narrow no-cluster
producer, not general clustered/semantic output.
All three merge-operand trees must satisfy the tracked sibling restriction;
staging a deletion does not remove that admission requirement.

Only the default full index on POSIX is admitted initially. Alternate, sparse,
split and unresolved indexes refuse. Output entries marked `assume-unchanged`
or `skip-worktree` refuse preparation and cancellation without clearing their
flags; unrelated index flags remain unchanged. Preparation and cancellation
preserve the existing index access mode and group when replacing it. A failure
to preserve that metadata refuses before index publication. `GIT_OBJECT_DIRECTORY` and
`GIT_ALTERNATE_OBJECT_DIRECTORIES` also refuse, so normal index entries cannot
depend on temporary object-storage environment settings. Both current executable
standalone Graphify merge-guard prehooks are required; install them with
`graphify hook install --merge-guard`. Foreign user prehooks refuse. Executable
managed posthook sections must match the current portable-aware implementation;
optional absent or nonexecutable posthooks do not launch rebuilding.
Guard installation uses one canonical output selector for all coupled hooks,
including when the configured selector has trailing slashes.
Hook inputs are rechecked before publication. Finalization rebuilds from frozen
Python blobs through the existing strict Python AST extractor, stages exact
prepared blobs without filters, and preserves unrelated index entries.
Extraction stdout is bounded before JSON decoding by the smaller of the
configured graph limit and the portable 256 MiB limit. Diagnostics and execution
time are also bounded; an exceeded limit refuses before index publication.
The manual commit guard rechecks installed hooks, cached and working-tree output
conversion attributes (including Git `ident` expansion), current v1-origin merge admission, and the private
preparation record. The staged artifact objects and content ID must match that
record. A self-consistent bundle alone does not authorize this merge transition.
Case-aliased portable envelopes refuse at the ordinary merge guard boundary.

Publication is index-only. No local graph, receipt or coordination is rewritten.
Post-events verify committed portable output and suppress the legacy rebuild
route for that operation while continuing preserved user hook commands.
The observer runs before linked-worktree legacy rebuild suppression. Without an
envelope, it scans the committed graph for the portable marker through EOF using
bounded buffers, without materializing node and edge collections. Input, token,
nesting and transport limits refuse observation when exceeded.
Ordinary pre-commit checks with staged portable authority require a valid bundle
for the staged Python projection and unchanged output conversion rules. A source
change without a matching bundle refuses; this adds no portable refresh route.
Publication also applies the reader's ancestor-authority rules to the indexed
or committed namespace: coordination names and prefixes, portable envelopes,
and ancestor graph authority must not make a fresh clone unreadable. Implicit
tracked directories participate in this check. Untracked local authority is
not an input to this prospective-tree check; the working-copy reader checks it
separately.
The producing checkout may therefore retain pending
or conflicting local authority; report that condition separately and use a fresh
clone for portable reading. Reconciliation failure cannot undo a recorded commit.
Identical-input retries must accept the complete staged portable representation;
changed inputs require fresh admission or explicit refusal.

An explicit `graphify merge-finalize --output graphify-out --cancel` restores
only the saved output index entries, preserving unrelated current staging and
working-tree content. A bounded preparation record under Git's per-worktree
administrative directory binds those entries to the selected merge and prepared
bundle. The complete record is flushed and published atomically without replacing
an existing record; an interrupted partial write cannot become retry authority.
Before replacing the index, preparation also retains every saved output blob
through a Git tree under `refs/graphify/merge-finalize/`. Its name binds the exact
private record, including worktree identity. The ref is shared so garbage
collection from another worktree retains those objects. The private record is
published first, while the original index still retains them. Cancellation
restores the index before deleting only the matching ref and record. A missing
ref can be recovered during cancellation only after all saved blobs, the pending
digest, and the current record have been validated; a foreign ref refuses.
Identical prepared retries and commit guards require the matching retention ref.
Successful commits retain the existing private record and its ref for deferred
local reconciliation; read-only post-event observation does not remove them.
It grants no local graph authority. The operator can then use ordinary
`git merge --abort`; unrelated worktree changes may still require resolution.
Plain Git abort may refuse
while the portable index and pending working copy differ; there is no automatic
rollback or promise that editor/signing cancellation removes prepared staging.

Portable-parent operands remain unsupported. A second merge must refuse without
claiming recurring lifecycle readiness. Supporting those operands, byte-preserving
local convergence/adoption, and broader sibling inventories requires separately
selected extensions. They are not hidden acceptance requirements of B1 or this
bounded transition, and B2 alone does not establish general issue #105 completion.

### Read authority and enrollment

The envelope binds the output selector, graph and manifest, exact artifact
paths and modes, byte digests, source-projection digest, extraction contract,
and content identifier. It excludes itself from its payload digest inventory;
Git's tree binds the envelope. Predecessor and merge-input bindings belong to
the preparation record; expose them in the envelope only where portable reader
validation requires them. Digests prove consistency, not trusted authorship.

Portable reads and local managed reads are explicit authority modes. A portable
bundle grants no write, recovery, lease, or token authority. Malformed, pending,
or conflicting local coordination must refuse; it must not fall back to the
portable reader. Unknown envelope versions and mixed incompatible formats
refuse. Existing local receipt, identity, drainer, and ownership checks remain.

Content identity is independent of local transaction generation. Fresh-clone
reading needs no local adoption or rebuild. Later explicit local adoption may
create local authority, but must preserve every committed artifact byte and
mode; rewriting a graph watermark to fit a new local generation is not adoption
of the same bundle. Adoption is not part of the first successful slice unless
its interaction with the chosen reader requires it and the owner selects it.

Enrollment is explicit: declare the output and portable inventory, prepare a
validated complete bundle, then stage only that declared closure. Missing
members, pre-existing incompatible tracked coordination, and unsupported sibling
formats refuse without conversion. Graph-only tracking receives no fresh-clone
guarantee until enrollment supplies its required closure. Never strip managed
watermarks to route managed content through a legacy reader.

The selected compatibility default is: new readers retain existing valid v1
behavior and add the explicit portable mode; old readers must reject the new
managed format rather than silently accept it as legacy. Prove that rejection
against supported old versions before selecting the encoding. Repositories must
select a minimum reader version for enrollment; automatic backward-compatible
reading and automatic migration are not promised.

### Inputs, artifact construction, and exact staging

Run a supported user pre-commit hook before freezing finalizer inputs. Capture
the effective index, including `GIT_INDEX_FILE`, resolved entries and modes,
`HEAD`, merge parents, repository/worktree identity, output selection, and the
extraction/configuration contract. Resolve Git paths through Git. Refuse
unresolved entries and unsupported index modes; an alternate index needs explicit
proof rather than assumptions about `.git/index`.

Build from immutable Git blobs for the selected source projection. Exclude the
output namespace. Working-tree, untracked, environment, and scratch-directory
configuration must not enter implicitly. Declare ignore rules, configuration,
submodules, symlinks, filters, and path normalization as admitted inputs or
unsupported cases. The first slice may refuse unsupported cases. A scratch
directory's absolute path must not alter committed content identity.

Admission still requires exact predecessor receipt and operand bindings, pending
payload digest, canonical merged content, generation, and pinned local identity.
The pending marker alone is insufficient. Do not trust a Git-merged manifest as
an incremental baseline.

Construct a complete successor `PublicationPlan`: graph, regenerated manifest,
supported siblings, and exact deletions. Each sibling has a declared semantic
rule: regenerate from frozen inputs, preserve with a justified compatibility
condition, or refuse. A matching predecessor digest alone does not prove that
an old report remains correct for changed sources. Unsupported semantic/custom
siblings therefore refuse in the smallest successful slice.

Prepare a replacement index from the captured index. Change only exact admitted
paths using prepared bytes and modes; never stage by directory or filename
prefix. Preserve unrelated entries and supported flags. Revalidate inputs and
replace the effective index under its proper lock. Filters, CRLF conversion,
case collisions, sparse indexes, and special modes need proof or refusal.
Revalidate after supported user hooks that can still affect the commit.

Successful post-event handling verifies the recorded tree and may reconcile
local output to its exact bytes only under preserved preconditions. It must
not rebuild different bytes, amend, or create a follow-up commit. Local
reconciliation failure is reported separately from committed-bundle integrity.

### Interruption, cancellation, and retry

| Boundary | Required outcome |
| --- | --- |
| Admission or private preparation failure | No Graphify public-output or index publication; preserve unrelated and user-hook changes. |
| Process interruption during index replacement | Original or complete replacement index under the supported host contract; no accepted mixed bundle. |
| Editor, later hook, or signing cancellation after replacement | No commit; a complete staged bundle may remain. No promise of automatic rollback. |
| Retry with identical captured inputs | Reuse only a complete validated bundle; do not duplicate Graphify finalization through nested hooks. |
| Retry with changed inputs | Fresh admission and preparation; preserve foreign edits and refuse uncertain recovery. |
| Interrupted local reconciliation | Exact journaled recovery or explicit refusal; no falsely active partial generation. |

Git may execute user hooks again on a new commit attempt. Graphify does not
promise once-only user-hook side effects across retries. Process-interruption
proof does not establish power-loss durability or isolation from arbitrary
non-cooperating writers. Record the supported filesystem and writer assumptions;
do not infer native Windows/MSYS qualification from POSIX results.

## Dependency boundaries

| Scenario | Dependency |
| --- | --- |
| Graph changes; predecessor managed siblings remain exact and supported | #105 can proceed without changed-sibling admission from #102. |
| Managed siblings are changed, added, deleted, or manually resolved | Relevant #102 admission policy is required, or #105 refuses. |
| Successor artifacts are regenerated after valid predecessor admission | #105 construction; this does not relax #102 predecessor checks. |
| Rebase | Separate #104 delivery with per-step immutable operands and conflict/continue/abort behavior. |
| Cherry-pick | Separate #104 delivery with its own operation identity, durable terminal handling, and conflict/continue/abort behavior. |

For #104, the proposed default is readable, internally consistent bundles at
every recorded step and source-fresh bundles at the completed sequence. Whether
every intermediate commit must also be source-fresh is an explicit acceptance
choice. Do not silently turn it into an existing requirement or permit pending
intermediate commits through an undefined exception. Terminal working-tree
recovery alone does not prove the contents of earlier recorded commits.

## Acceptance matrix and remaining choices

Every success claim inspects Git's recorded tree and reads it from a fresh clone
without original checkout state, hooks, rebuild, or adoption. Every refusal claim
compares the effective index and protected working-tree bytes before and after.

| Scenario | Required proof |
| --- | --- |
| Automatic merge containment | Actual consumed tree identified; pending commit prevented without Graphify staging. |
| Manual merge success | Recorded complete envelope/inventory matches frozen source projection; fresh-clone readers succeed. |
| Unstaged/untracked differences | Excluded from graph; unrelated index and working-tree content preserved. |
| Receipt/artifact tampering or mixed local authority | Refusal without fallback, conversion, or mutation. |
| Default/custom output, linked worktree, hooks path | Correct selector, effective index, and worktree; no authority borrowed from another checkout. |
| Cancellation, interruption, retry, bypass | Outcomes match the boundary table; no hidden commits or foreign cleanup. |
| Reader compatibility | New reader accepts enrolled closure; supported old readers reject it safely; valid v1 behavior preserved. |
| Rebase/cherry-pick | Separate #104 matrices inspect intermediate and final trees according to the selected integrity/freshness policy. |

The selected profiles above resolve the initial encoding, reader contract,
enrollment, sibling and index boundaries. Release/build qualification and actual
host evidence remain required before claiming support. These choices cannot be
inferred from a guard passing its tests. The earlier baseline evidence proves
the defect and Git ordering only; it does not establish Delivery B acceptance.

B1 acceptance includes a complete committed closure in an isolated fresh clone,
an expected query answer, source/configuration omission and tampering, mixed local
authority, orphan envelopes, marker-only states, duplicate JSON members, aliases,
mode and path changes, bounded inputs, and old-reader rejection. Preservation
checks include bytes, modes, index and query-log locations with logging enabled;
Git status alone is insufficient. Artifact integrity, source-projection binding,
and producer extraction correctness are separate claims.

B2 acceptance additionally inspects the actual recorded tree, source changes
excluded from frozen inputs, unrelated staged entries, cancellation, interruption,
retry, predecessor/operand mismatch, unsupported hooks/indexes/siblings, post-event
rebuild suppression, and explicit refusal of unsupported subsequent operations.
No test result qualifies another host or stronger writer-isolation assumption.

### Local implementation evidence

The initial candidate, published as `055699eb`, was checked on macOS with Python 3.14.3 and Git 2.55.0,
against source baseline `3cecae9bed3f9cb02caa70788fae8faa583f8828`.
These are local receipts, not a release qualification or hosted CI result.

- Final portable admission suite: 102 passed, including malformed query fields,
  serialized runtime metadata, immutable snapshots and complete source selection.
- Final manual-finalization suite: 28 passed in 50.98 seconds. It includes a real
  manual merge commit, actual portable query in a fresh clone, unchanged local
  output, exact cancellation, unrelated index flags/stat preservation, filter
  non-execution, interruption/retry and changed Git/hook identity refusal.
- Final portable CLI/routing, existing query CLI and README-policy selection:
  190 passed. Detached merge/driver regression selection: 13 passed.
- Earlier transaction/portable integration selection: 1,043 passed and one
  skipped in 54.08 seconds. Later boundary corrections were rechecked with the
  focused selections above. Existing hooks and merge-guard/lifecycle selection:
  499 passed and seven skipped in 186.23 seconds.
- Independent cumulative source review and ordered correction review closed all
  supported findings. The reviewer did not execute tests.
- Ruff and the medium/high Bandit selection passed. Pyright passed for
  `portable.py`, `merge_finalize.py` and `merge_guard.py`. Earlier broader
  touched-source typing found three errors also reproduced from the unchanged baseline:
  stream `reconfigure`, dispatch complexity and a generator return annotation.
- Required AST graph refresh completed. Missing optional SQL/DM parsers and six
  empty extraction results limit graph coverage; the graph exceeded the HTML
  visualization limit. These warnings do not qualify those omitted inputs.

The pre-PR full gate then ran after `uv sync --all-extras --frozen`:
`uv run --frozen pytest tests/ -q --tb=short --durations=15`, with an external
trusted `--basetemp`. It passed with 8,484 tests, 47 skips, 235 passing subtests
and six warnings in 3,982.37 seconds (1:06:22). The warnings were three Jieba
escape-sequence warnings, two out-of-scope semantic-cache warnings and one
managed-state cache-cleanup warning. The slowest individual test was the existing
installed S5 native round-trip/cold-read case at 71.94 seconds; the other top
timings were existing S4 workspace authority, admission and cleanup tests.

Generated-skill validation matched all 134 artifacts. Optimized verifier
conformance passed 80 tests with the expected Python-optimization warning.
Isolated installation and native Leiden weighted-partition smoke checks passed.
Scoped Pyright and Ruff checks also passed in the all-extras environment.

Hosted CI has not been awaited. The compatibility fixture exercises
the baseline transaction-reader module with unchanged current dependencies;
it does not qualify every historical installed distribution. Recurring merges,
portable adoption and the remaining lifecycle issues retain their separate scope.

## Source pointers

- [`portable.py`](../../graphify/portable.py): portable envelope, source projection,
  immutable read result and committed-tree admission.
- [`merge_finalize.py`](../../graphify/merge_finalize.py): bounded manual staging,
  private cancellation record and committed-output observation.
- [`PublicationPlan`](../../graphify/transaction.py): complete payload inventory
  and exact deletions.
- [`_validate_receipt_locked` and `open_graph_snapshot`](../../graphify/transaction.py):
  current local receipt, artifact, watermark, and directory-identity admission.
- [`open_external_graph_snapshot`](../../graphify/transaction.py): unmanaged
  reader rejects managed watermark/coordination authority.
- [`hooks.py`](../../graphify/hooks.py) and
  [`hook_installation.py`](../../graphify/hook_installation.py): current hook
  recovery and installation boundaries.
- [Hook installation recovery](../hook-installation-recovery.md) and
  [consolidation status](graphify-product-consolidation-status.md): existing
  bounded guarantees and separate future deliveries.
