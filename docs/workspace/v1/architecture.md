# Architecture

## Boundary

Graphify remains one distribution and one graph engine. Native `0.9.16` owns
detection, structural and semantic extraction, caches, graph construction,
clustering, export, diagnostics, watch/update behavior, and traversal. The
workspace control plane will own external lifecycle state around that engine.

P1 freezes only these seams:

1. repo policy and stable UUID label;
2. registry and one explicit active source;
3. immutable-generation receipt and sealed query payload;
4. external framed/hash-linked journal events;
5. fenced leases and pointer records;
6. retained generation coordination-lock identity;
7. two-sided observation-based freshness release record;
8. compatibility and candidate artifact manifests; and
9. installer transaction, compensation, and offline rollback records.

P2 implements the registry writer, identity/source enrollment, explicit
active-source CAS, and lease allocator. P3 implements lifecycle mechanics for
caller-supplied staged generations, pointers, journals, and offline GC. P4
implements the sole `0.9.16` adapter, no-write comparison seam, and
observed-current release authority. P5A adds only the durable semantic desired-
work queue, fenced worker claims, reconciliation evidence, and the generation-
certification binding to one stable queue watermark. Pre-workspace state has no
import or promotion lane. P5B1 adds the production composition root and the
versioned read-only `graphify workspace status --json` and
`graphify workspace doctor` surface without repair or durable-state mutation.
The executable surface discovers only the standard external state root and
reads one bounded, canonical `runtime-manifest.json`; it never searches a
checkout, synthesizes a compatibility tuple or queue policy, or derives policy
from durable queue content. Missing or unusable authority fails closed.
P5B2a adds only explicit initial enrollment and explicit adoption through
`graphify workspace register`. The command composes that installed authority,
requires matching stdin authorization and an expected registry revision,
discovers the current Git top level, then delegates to the P2 registry CAS.
Adoption is never inferred: the operator must select `adopt`, and the existing
registry must verify shared history for the same UUID. Rebind, rotation, activation,
remaining mutation/query commands, repair, watch/service, installation,
performance certification, candidate publication, and live-cutover work remain
deferred; retained production query/service authority remains P5C work.

## Authority split

Repo-owned `.graphify/workspace.toml` contains the required
`contract = "graphify.workspace.config"` discriminator, `schema_version = 1`,
the immutable `repo_uuid` label, and policy only. It cannot select lifecycle
paths, pointers, generations, or global state. Enrollment remains
operator-authorized; possession of a copied UUID is not proof of identity.

The P2 global registry has one singular `active_source` per workspace plus
a monotonic `active_source_revision`, UUID-enrollment evidence, and
revision/source-bound active-source evidence. The active-source evidence stamps
the distinct operation epoch and accepted workspace-operation fence token that
a future activation CAS must revalidate. Paths, worktree coordinates, and
normalized remote URLs remain discovery aliases, not stable identity. A query
never guesses among aliases.
Semantic claim capability is derived only from the validated configuration read
through that registry-selected active source; a caller configuration must match
it canonically, because a matching UUID label alone is not policy authority.

P2 writes only this external lifecycle state:

```text
~/.local/state/graphify/
  registry.json
  registry.previous.json
  registry.pending.json
  registry.lock
  evidence/<sha256>.json
  workspaces/<repo_uuid>/
    workspace.json
    workspace.previous.json
    workspace.pending.json
    workspace.lock
```

P5B1 additionally reserves `runtime-manifest.json` at the root above as an
internal format-version-1 read authority containing the complete frozen
compatibility manifest and explicit semantic-queue policy. Status, doctor, and
P5B2a registration authority loading read it through the same private-directory,
singular-regular-file, 0600, no-follow, bounded-read rules used for durable
state. Registration then writes only the P2 paths already shown above. P5C owns
creating and atomically installing the candidate-backed authority; P5B1/P5B2a
do not create it or guess its contents.

P3 now owns generations, lifecycle journals, coordination locks, pointers,
capacity reservations, and explicit offline-GC records. None of those paths is
written inside a source checkout.

P5A adds this per-workspace state under the same external lifecycle root:

```text
~/.local/state/graphify/workspaces/<repo_uuid>/queue/
  semantic.jsonl
  semantic.previous.jsonl
  semantic.pending.jsonl
  certifications/<generation_id>.json
```

Despite the retained `.jsonl` path name, each file is one canonical internal
JSON record handled by the existing current/previous/pending durable-record
protocol. The record is not a new public v1 schema and does not change the
frozen receipt schema. Its active-source revision, monotonically increasing
desired and completed watermarks, exact desired-set reconciliation, claim state,
retry/dead-letter state, typed repeated-source-observation evidence, sealed
staged-input binding, and compaction epoch are external lifecycle state, never
source-checkout content. A source activation requires a newer exact
reconciliation before retained desired work is eligible again.
Each certification file is a separate immutable, store-owned internal record
binding one generation and certification request to the revalidated queue view
and exact sealed staged-input manifest.

## Ordering

P2 enforces global registry lock before the exclusive fenced workspace-operation
lock whenever an operation needs both. Activation holds both in that order.
Every public lease transition holds the registry lock while recovering one
stable snapshot, then nests the per-workspace lock through validation and any
lease-state commit. A durable pending registry revision therefore cannot be
skipped between recovery and workspace CAS. The locks are released before the
long-lived operation begins, so builds remain serialized only for their short
lease transition rather than for extraction work. P3 extends the nested order
with generation coordination locks in lexical generation-ID order and pointer
validation/CAS. P4 queries hold the existing registry lock shared, then the
pre-created generation lock shared; they do not acquire a writer-operation
lease and create no coordination object.

P5A queue mutation reuses the registry-before-workspace order through the
existing lease authority. Desired-work reconciliation uses a lifecycle lease;
claim, checkpoint, completion, failure, and semantic-lease compaction use the
reserved `SEMANTIC_CLAIM` domain while retaining the current registry revision
through the workspace mutation. Claim resolves the selected source before those
locks, then compares that identity and a fresh safe config read against the
still-locked registry entry, so activation cannot authorize work from a retired
policy. After exact reconciliation and semantic completion, a lifecycle lease
binds the queue watermark to the exact staged-payload manifest. Certification
first captures that queue view from two equal typed source observations under
the accepted build or migration lease, then revalidates the exact queue revision
and canonical-state hash while `GenerationStore` holds the workspace lock and
before it takes the generation lock. It then durably installs an immutable
store-owned certification binding before the generation lock or staged receipt
can become authority. A queue transition between capture and binding therefore
blocks certification instead of producing a receipt against mixed evidence.
Once the binding is durable, the exact request and payload can recover even if
newer desired work later advances the queue; a staged receipt alone is never
queue authority. The receipt remains the idempotent boundary for the subsequent
generation install and journal transition.

Activation, migration, promotion, rollback, repair, pointer recovery, and GC
share one fenced workspace-operation domain. `SEMANTIC_CLAIM` has its reserved
semantic domain. Each live domain retains its own accepted operation epoch, so
allocating a semantic claim cannot invalidate or strand an otherwise-current
workspace lease. Migration may invalidate semantic commit authority, but the
exact trusted owner/fence can still release that stale record. P2 allocates and
validates these leases; P3 consumes only the lifecycle-operation subset. P4
owns adapter and freshness use. P5A consumes the semantic domain and binds
stable semantic completion to certification. Later P5 slices remain responsible
for orchestration, services, commands, installation, and publication.
