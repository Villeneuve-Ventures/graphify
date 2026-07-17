# P4 adapter and freshness runtime

P4 is a library-only boundary around the frozen P2/P3 control plane. It adds
one concrete engine adapter for published Graphify `0.9.16`, read-only retained
`0.9.12` state interpretation, and two-sided observed-current query release.
It does not add a semantic queue, workspace command, service, installation
path, route switch, or repository migration.

## Adapter and compatibility boundary

`graphify.workspace.adapters.v0_9_16.Graphify0916Adapter` is the only concrete
v1 adapter. Graphify-private build, detection, extraction, security, and query
imports are confined to that versioned module. The adapter tuple is derived
from one validated canonical compatibility manifest; its SHA-256 is the same
digest checked on the selected generation receipt.

The exact supported tuple is `graphifyy 0.9.16+workspace.1`, engine baseline
`0.9.16`, extractor/cache ABI `graphify-0.9.16`, adapter contract `1`, and state
schema `1`. Any mixed or unknown tuple rejects before an adapter can be used for
execution, staging, query, or promotion. An internally coherent future
whole-artifact tuple can be classified only by the explicit `PROBE` intent. It
has no executable adapter and cannot stage or promote state.

The retained reader accepts only declared source version `0.9.12`. It hashes
and validates the legacy manifest, AST/semantic cache entries, and graph/report
artifacts without changing their bytes or metadata. The returned manifest
entries preserve AST and semantic hash attribution. Live legacy-writer
quiescence, copying into a generation, and repository migration remain later
work.

## No-write detection seam

`graphify.detect.detect(..., read_only=True)` is the fork-owned comparison seam.
It bypasses persistent word-count/stat caches and suppresses Office and Google
Workspace conversion sidecars. Office inputs remain visible as their original
source files for hashing. Google Workspace shortcuts are surfaced as an
unsupported comparison because authoritative remote content cannot be observed
without export or network effects.

Ordinary detection given an explicit `cache_root` redirects conversion
sidecars beneath that output root. The P4 structural build uses read-only
detection, runs the `0.9.16` extractor/build implementation, writes only to its
explicit external output root, and reports non-code dispatched inputs that it
did not structurally extract.

## `current_only` release protocol

`FreshnessAuthority` implements this sequence:

1. Validate the compatibility manifest and select the exact adapter before
   reading registry, pointer, generation, or source state.
2. Hold the existing registry lock shared, resolve its singular active source,
   then open the current generation through its pre-created shared coordination
   lock. This preserves registry-before-generation lock ordering.
3. Discover source identity and collect a complete no-write pre-query
   observation. Each selected file is opened without following the final
   symlink, hashed through one descriptor, and checked for identity and metadata
   stability. Complete inventory passes repeat until two consecutive results
   agree.
4. Compare pointer/source revisions, operation and fence values, schema,
   source commit, inventory, policy/ignore digest, detector identity, receipt,
   payload manifest, and compatibility digest to the sealed generation.
5. Run the adapter's native `0.9.16` traversal against the immutable payload.
   This library path deliberately bypasses optional query logging.
6. Repeat source identity and the complete observation, revalidate source
   identity once more, and revalidate the pointer/receipt while both shared
   locks remain held.
7. Release output only when both observations equal each other and the sealed
   tuple. Drift, unstable inventory, timeout, unsupported comparison, corrupt or
   unavailable authority, and pointer change discard output.

The callback runner is private and exists only for deterministic fault
schedules. The supported query surface is the no-log adapter path described
above, so callers cannot substitute a stateful payload function.

`current_only` is an observation protocol, not an atomic checkout snapshot. It
does not claim strict source linearizability or detection of an edit that is
fully made and reverted between the two observations. A change after the
documented release-observation boundary is a subsequent change outside that
decision. Watchers and future queues are accelerators only; P4 freshness does
not consult them, so an outage or missed event cannot authorize stale output.
