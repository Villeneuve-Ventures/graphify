# P4 adapter and freshness runtime

P4 is a library-only boundary around the frozen P2/P3 control plane. It adds
one concrete engine adapter for published Graphify `0.9.16` and two-sided
observed-current query release. It does not add a retained-state import path,
semantic queue, workspace command, service, installation path, route switch,
or repository migration.

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

Pre-workspace `graphify-out` trees have no import or promotion authority. Later
adoption must build and certify a new generation through the exact supported
tuple while leaving any pre-workspace tree untouched.

## No-write detection seam

`graphify.detect.detect(..., read_only=True, comparison_reader=...)` is the
fork-owned comparison seam. Certified callers must supply the descriptor-safe
reader used for classifier probes and every effective ignore/include policy
read; there is no fallback to ordinary `Path.open` or `read_text`. It bypasses
persistent word-count/stat caches and suppresses Office and Google Workspace
conversion sidecars. Office inputs remain visible as their original source
files for hashing. Google Workspace shortcuts are surfaced as an unsupported
comparison because authoritative remote content cannot be observed without
export or network effects.

The adapter pins the allowed source/VCS root for the complete detection and
inventory pass. A linked worktree additionally bootstraps its exact Git-dir and
shared common-dir from the rooted `.git` and `commondir` files, pins those
directories separately, and allowlists only the routing files plus shared
`info/exclude`; other external targets reject. Classifier probes, selected
source files, and effective policy inputs are opened relative to that bounded
read authority, with every ancestor and final entry opened without following
links. Source and policy-root bindings are revalidated throughout and at pass
completion.

Ordinary detection given an explicit `cache_root` redirects conversion
sidecars beneath that output root. The P4 structural build uses read-only
detection, streams every selected code file through the descriptor-safe reader
from a pinned source-root descriptor into an ephemeral external snapshot.
Every ancestor is opened descriptor-relative without following links, and the
installed rooted path is revalidated after hashing. The `0.9.16`
extractor/build implementation runs only against that snapshot in an ephemeral
private build directory. Per-file extractor errors reject the build, and the
payload is constructed as a directed graph so reciprocal relationships remain
distinct. The adapter requires existing, real-directory ancestry
for the requested output root, opens it descriptor-relative without following
links, and keeps it pinned while copying the normalized result. The source read
authority remains open through publication, and both source and output bindings
are revalidated immediately before and after the descriptor-relative copy. The
content digests recorded for every snapshotted code file and effective policy
input are also re-read through their pinned descriptors at both boundaries, so
an in-place edit cannot publish a graph derived from stale source or policy
bytes. The destination must still be empty immediately before publication and
must contain exactly the published `graphify-out` tree afterward, so an ancestor
replacement
or concurrent destination entry cannot redirect or contaminate writes. The
adapter persists the queryable
`graphify-out/graph.json` artifact there, normalizes the staging root to `0700`,
directories to `0755`, and files to `0644`, and reports non-code dispatched
inputs that it did not structurally extract. The snapshot preserves the
source-relative tree, including the XAML project-resolution anchor, and both
ephemeral trees are removed before the build returns.

Detection, snapshotting, and publication retain the same pinned source authority. Every
selected code file and its ancestor identities are recorded after detection and
must still match before any snapshot read, so a real-directory replacement in
that boundary or before publication fails without reading the replacement inode.

## `current_only` release protocol

`FreshnessAuthority` implements this sequence:

1. Validate the compatibility manifest and select the exact adapter before
   reading registry, pointer, generation, or source state.
2. Hold the existing registry lock shared, resolve its singular active source,
   then open the current generation through its pre-created shared coordination
   lock. This preserves registry-before-generation lock ordering. A caller
   deadline bounds both acquisitions through nonmutating nonblocking polling;
   the same deadline is checked between registry, pointer, receipt, journal, and
   release revalidation reads. Expiry withholds as `timeout` before the next
   protected phase or output release.
3. Discover source identity and collect a complete no-write pre-query
   observation. Detection and hashing share one pinned read authority spanning
   the checkout plus the bounded per-worktree and common Git metadata roots.
   Classifier, source, policy, and Git HEAD/ref inputs are traversed
   component-by-component without following links, read through their pinned
   descriptors, and checked for identity, rooted-path, and metadata stability.
   Complete inventory passes repeat until two consecutive results agree.
4. Compare pointer/source revisions, operation and fence values, schema,
   source commit, inventory, the digest of every effective policy input
   (including Git `info/exclude` and ancestor policies), detector identity,
   receipt, payload manifest, and compatibility digest to the sealed generation.
5. Recheck the deadline, then run the adapter's native directed `0.9.16`
   traversal against the immutable payload through the same rooted no-follow
   reader. Requests reject malformed field types before comparison or lock
   acquisition, and reject when depth exceeds `8`, token budget exceeds `32768`,
   question text or term work exceeds its bound, or context filters exceed their
   count, item, or aggregate bounds. This library path deliberately bypasses
   optional query logging.
6. Repeat source identity and the complete observation. Completion of this
   post-query source observation is the source release-observation boundary.
   Revalidate registry source identity and the pointer/receipt while both shared
   locks remain held; those authority checks do not extend source
   linearizability beyond the completed observation.
7. Release output only when both observations equal each other and the sealed
   tuple. Drift, unstable inventory, timeout, unsupported comparison, corrupt or
   unavailable authority, and pointer change discard output.

The callback runner is private and exists only for deterministic fault
schedules. The supported query surface is the no-log adapter path described
above, so callers cannot substitute a stateful payload function.

`current_only` is an observation protocol, not an atomic checkout snapshot. It
does not claim strict source linearizability or detection of an edit that is
fully made and reverted between the two observations. A change after the
completed post-query source observation is a subsequent change outside that
decision. Watchers and future queues are accelerators only; P4 freshness does
not consult them, so an outage or missed event cannot authorize stale output.
