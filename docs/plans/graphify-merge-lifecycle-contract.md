# Merge lifecycle contract for #105, #102, and #104

Status: proposed contract, 2026-10-08. Source baseline: v8
`0373e6d74675891c83f4cf7bbe5c5e5d5311ed4a`.
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
The proposed default is a small versioned portable envelope, reusing bounded
inventory validation, while retaining the existing local capability protocol.
Its final filename and encoding remain design decisions, not delivered APIs.

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

The proposed compatibility default is: new readers retain existing valid v1
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

Before Delivery B, the owner must select the envelope encoding and minimum reader
version, enrollment interaction with existing tracked local coordination, the
first supported sibling policies, and exact host/index support. The defensible
default is explicit enrollment, unchanged local protocol, no automatic conversion,
and refusal for unsupported cases. These choices cannot be inferred from a guard
passing its tests. The current-baseline evidence above proves the defect and Git
ordering only; it does not establish Delivery B acceptance.

## Source pointers

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
