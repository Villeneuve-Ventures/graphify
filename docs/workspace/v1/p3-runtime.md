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

GC requires a live fenced `GC` operation and an explicit `GcProtection` set for
migration, rollback, lease, fixture, proof, and rollback-artifact reachability
that P3 cannot infer. The dry run writes nothing. Execute persists an intent,
takes exclusive generation locks in lexical order, rechecks pointer and caller
protections, atomically renames only unreachable generations to quarantine,
syncs both directories, and persists completion. An unresolved GC intent is a
workspace-wide mutation barrier until a successor `GC` or `POINTER_RECOVERY`
fence reconciles source/quarantine location. A durable pointer intent likewise
blocks every mutation except fenced pointer recovery. GC intent, completion,
and purge evidence are bound to the containing workspace and plan. Recursive
deletion is a separate explicit purge; coordination lock identities remain
retained. Purge deletion and parent-directory synchronization use the injected
syscall seam so partial deletion, interruption, and uncertain sync are retried
before one durable purge record is accepted.

Capacity values used by tests are deterministic fixtures only. P3 deliberately
defines no operational default or public v1 config field for those limits.
