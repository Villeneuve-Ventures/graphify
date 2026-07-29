# P3 generation and recovery runtime

P3 is a library-only control-plane layer around caller-supplied staged
`graphify-out` payloads. It never invokes extraction, update, watch, query,
semantic, adapter, service, install, or routing behavior.

## Durable order

Allocation validates an explicit internal `CapacityPolicy` during a short
registry-plus-workspace critical section. Global reservations are serialized
in external state before staging is created, but payload construction and
certification retain only the owning workspace lock so unrelated workspaces do
not serialize for the duration of sealing. A successor fence may adopt an
unfinished reservation only when its generation, byte count, policy, and
active-source revision are identical. Activation remains blocked until that
reservation is completed and cleared. Each generation receives a retained
canonical coordination object before its first journal transition.
Filesystem reserve preflight subtracts the unconsumed headroom promised by
every durable reservation before admitting another allocation.
If certification completed before a process died after durable reservation
release, a successor verifies the installed receipt and journal and returns the
same certified result without recreating staging or capacity state.
Capacity enumeration requires two identical logical filesystem observations;
it retries bounded rename/purge races and treats persistent duplicate locations
or a snapshot that cannot stabilize as a capacity failure.

Certification inventories every payload file by no-follow descriptors, hashes
stable before/after identities, checks the caller's exact canonical manifest,
syncs files in path order and directories bottom-up, installs and syncs the
receipt, atomically renames the complete generation, reopens it, and appends
`CERTIFIED`. Certified payload and receipt bytes are never rewritten.

The journal stores one GWF1 frame per numbered sealed segment. Its canonical
head binds the last sequence, event hash, segment name, and segment hash. A
single complete hash-linked next segment is adopted after process death; a
single incomplete next frame is discarded. Private atomic-replace temporary
files left by process death are removed only after their exact owner, mode,
type, link count, and name are validated. Committed truncation, checksum
failure, gaps, extra suffixes, divergent idempotency, or a mismatched head fail
closed. Every decoded event ID is recomputed with the containing workspace UUID,
so a segment copied from another workspace cannot be adopted. `occurred_at` is
canonical logical-event material: a retry under the
same operation epoch and fence must reuse the exact timestamp, while a
successor fence records a distinct event. Generation certification and visible
pointer transitions are appendable only through their owning stores.
If a predecessor dies after `VALIDATING` but before sealing a receipt, the
successor appends a strictly newer fenced `VALIDATING` event before creating
its receipt. A fully sealed predecessor receipt may instead be adopted only
after its original validating event and complete contents are reverified.

Pointer movement validates the full caller-supplied CAS tuple, locks involved
generations in lexical order, durably retains the prior pointer, persists a
recovery intent, and performs one atomic visible replacement. Repair chooses
only a fully verified receipt and always writes a revision above every observed
pointer/journal revision. It binds retained and visible pointers to their
containing workspace, revalidates current active-source evidence, quarantines
only generations whose receipts or payloads are corrupt, and never reinstalls
an old revision directly. Recovery rejects stale pending/current records against
the retained-prior and journal revision evidence, and it resumes its own fenced
pending repair only after fully verifying every referenced generation.

`graphify workspace repair --dry-run --request-stdin` is the bounded public
inspection transport for that existing repair policy. It consumes one canonical
CLI-v1 request no larger than 16 KiB, uses existing-only registry/workspace and
generation locks, and projects the same bounded pointer/journal/generation
decision without creating a lock, allocating a lease/fence, recovering state,
cleaning temporary files, or writing anything. Its result is canonical and
redacted, classifies the decision as `no_op`, `repairable`, or `irreparable`,
and carries the only execute-approval bytes.

`graphify workspace repair --execute --request-stdin` consumes a separate
16 KiB canonical request with `REPAIR_EXECUTE` authorization and the SHA-256 of
the exact preview bytes, including the trailing newline. It recomputes the
preview, obtains a fresh CAS-bound `REPAIR` fence, and recomputes the exact plan
with mutation locks held before `PointerStore` may recover the journal, mutate
the pointer, or quarantine an excluded corrupt generation. An approved no-op
passes the same fence and in-lock plan comparison before returning. One
absolute request deadline bounds preview, fence acquisition, and the in-lock
mutation decision without a per-phase timeout reset. The plan is not an
arbitrary historical selector: its candidate must be a fully verified,
journal-certified generation under the current active-source authority, and its
revision is above all observed pointer/journal evidence. A stale or
commit-unknown execute is not replayable; status inspection and a fresh
preview/request pair are required.

## Readers and GC

A reader loads the pointer, opens the already-created coordination object
read-only, takes a shared kernel advisory lock, reloads the exact pointer, and
verifies the receipt and payload. Hidden pending-recovery intent does not mask
the complete visible pointer from readers; a reader whose pointer changed
before its lock stabilized retries against the new visible generation. This
read exception applies only after the journal durably records the exact visible
pointer revision; a visible-pointer-only crash boundary still fails closed. A
residual pending intent remains a mutation barrier until fenced recovery. The
reader acquires no writer lease and calls no mutating persistence primitive.

`graphify workspace gc --dry-run --request-stdin` is a separate public preview:
it loads installed authority before one canonical request, accepts all capacity
and six protection classes explicitly, uses read-only coordination and two
matching reachability snapshots, and emits an unfenced deterministic result.
It creates no lease, fence, `GcPlan`, state, lock file, cleanup, quarantine,
receipt, or log, including on failures. It neither infers capacity nor protections
and does not change the following fenced execution contract.

The public lifecycle is explicit-only: `gc --execute --request-stdin`,
`gc --reconcile --request-stdin`, and `gc --purge --request-stdin` each require
a distinct canonical authorization action. Execute and first-time reconcile or
purge mutation acquire a fresh trusted `GC` lease; matching current-epoch
completion recovery, reconcile with no recovery state, and exact terminal purge
replay return no-write results without acquiring one.
Execute accepts an operator-approved SHA-256 of the exact canonical public
preview-result bytes, recomputes that preview before leasing, then creates a
fresh plan and requires its non-fence projection to equal the preview. The
comparison includes repo UUID, registry and active-source revisions, migration
epoch, pointer revision, capacity-policy digest, candidates, and protected
generation reasons. A `shared_lock` reason is redundant only when another reason
already protects that generation; a sole lock reason remains material. The
newly allocated fence and operation epoch are excluded. The request's absolute
deadline continues through planning, blocking generation-lock acquisition, and
the fenced mutation.
Reconcile names no plan, mutates only an existing GC intent, and otherwise
replays the completion indexed by its still-current operation epoch or returns
an explicit no-op. Purge requires an exact completed plan SHA-256 and remains
idempotent while rechecking protections and locks. Public receipts are
canonical and redacted; they never disclose authority, raw lifecycle documents,
fences, owners, paths, timestamps, operation epochs, or raw errors.

For preview, plan, and lifecycle observations, the public maximum is 4096
generation IDs. Directory enumeration validates each candidate descriptor
without following links and stops after the 4097th entry; it reports overflow
before materializing the generation collection. This constrains traversal
correctness only and makes no performance or resource claim.

`GcStore.plan()`, `execute()`, `reconcile()`, and `purge()` require a live
fenced `GC` operation and an explicit `GcProtection` set for migration,
rollback, lease, fixture, proof, and rollback-artifact reachability that P3
cannot infer. Their dry run writes nothing. Execute persists an intent, takes
exclusive generation locks in lexical order, rechecks pointer and caller
protections, atomically renames only unreachable generations to quarantine,
syncs both directories, and persists completion. An unresolved GC intent is a
workspace-wide mutation barrier until a successor `GC` or `POINTER_RECOVERY`
fence reconciles source/quarantine location. A durable pointer intent likewise
blocks every mutation except fenced pointer recovery or the bounded fenced
public pointer-repair execution. GC intent, completion,
and purge evidence are bound to the containing workspace and plan. Recursive
deletion is a separate explicit purge; coordination lock identities remain
retained. Purge deletion and parent-directory synchronization use the injected
syscall seam so partial deletion, interruption, and uncertain sync are retried
before one durable purge record is accepted.

An unresolved valid GC intent remains visible to status and doctor as the
operator action `run_workspace_gc_reconcile`. It does not cause the runtime to
reconcile automatically.

Capacity values used by tests are deterministic fixtures only. P3 deliberately
defines no operational default or public v1 config field for those limits.
