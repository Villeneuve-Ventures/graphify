# P3 generation and recovery runtime

P3 is a library-only control-plane layer around caller-supplied staged
`graphify-out` payloads. It never invokes extraction, update, watch, query,
semantic, adapter, service, install, or routing behavior.

## Durable order

Allocation validates an explicit internal `CapacityPolicy` while the registry
and workspace operation locks are held. Global reservations are serialized in
external state before staging is created. Each generation receives a retained
canonical coordination object before its first journal transition.

Certification inventories every payload file by no-follow descriptors, hashes
stable before/after identities, checks the caller's exact canonical manifest,
syncs files in path order and directories bottom-up, installs and syncs the
receipt, atomically renames the complete generation, reopens it, and appends
`CERTIFIED`. Certified payload and receipt bytes are never rewritten.

The journal stores one GWF1 frame per numbered sealed segment. Its canonical
head binds the last sequence, event hash, segment name, and segment hash. A
single complete hash-linked next segment is adopted after process death; a
single incomplete next frame is discarded. Committed truncation, checksum
failure, gaps, extra suffixes, divergent idempotency, or a mismatched head fail
closed.

Pointer movement validates the full caller-supplied CAS tuple, locks involved
generations in lexical order, durably retains the prior pointer, persists a
recovery intent, and performs one atomic visible replacement. Repair chooses
only a fully verified receipt and always writes a revision above every observed
pointer/journal revision. It never reinstalls an old revision directly.

## Readers and GC

A reader loads the pointer, opens the already-created coordination object
read-only, takes a shared kernel advisory lock, reloads the exact pointer, and
verifies the receipt and payload. Hidden pending-recovery intent does not mask
the complete visible pointer from readers; a reader whose pointer changed
before its lock stabilized retries against the new visible generation. It
acquires no writer lease and calls no mutating persistence primitive.

GC requires a live fenced `GC` operation and an explicit `GcProtection` set for
migration, rollback, lease, fixture, proof, and rollback-artifact reachability
that P3 cannot infer. The dry run writes nothing. Execute persists an intent,
takes exclusive generation locks in lexical order, rechecks pointer and caller
protections, atomically renames only unreachable generations to quarantine,
syncs both directories, and persists completion. An unresolved intent blocks
pointer writers until a successor `GC` or `POINTER_RECOVERY` fence reconciles
source/quarantine location. Recursive deletion is a separate explicit purge;
coordination lock identities remain retained.

Capacity values used by tests are deterministic fixtures only. P3 deliberately
defines no operational default or public v1 config field for those limits.
