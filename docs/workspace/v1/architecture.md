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
implements the sole `0.9.16` adapter, read-only `0.9.12` retained-state reader,
no-write comparison seam, and observed-current release authority. Semantic
queues, services, and the workspace CLI remain dependency-ordered P5 work.

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

P3 now owns generations, lifecycle journals, coordination locks, pointers,
capacity reservations, and explicit offline-GC records. None of those paths is
written inside a source checkout.

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

Activation, migration, promotion, rollback, repair, pointer recovery, and GC
share one fenced workspace-operation domain. `SEMANTIC_CLAIM` has its reserved
semantic domain. Each live domain retains its own accepted operation epoch, so
allocating a semantic claim cannot invalidate or strand an otherwise-current
workspace lease. Migration may invalidate semantic commit authority, but the
exact trusted owner/fence can still release that stale record. P2 allocates and
validates these leases; P3 consumes only the lifecycle-operation subset. P4
owns adapter and freshness use. P5 remains responsible for semantic, service,
and command use.
