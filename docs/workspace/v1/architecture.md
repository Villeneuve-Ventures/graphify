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

P2 now implements the registry writer, identity/source enrollment, explicit
active-source CAS, and lease allocator. It does not implement a generation
builder, pointer mover, journal appender, GC action, freshness scanner, semantic
queue, service, or workspace CLI. Those remain dependency-ordered P3-P5 work.

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

P3 owns generations, lifecycle journals, coordination locks, and pointers; P2
does not create those paths or write into source checkouts.

## Ordering

P2 enforces global registry lock before the exclusive fenced workspace-operation
lock whenever an operation needs both. Activation holds both in that order.
Ordinary lease allocation first recovers a registry snapshot, releases the
global lock, then takes the per-workspace lock and rechecks the atomically
installed current registry revision; unrelated workspaces are therefore not
globally serialized. P3 extends the nested order with generation coordination
locks in lexical generation-ID order and pointer validation/CAS. Queries will
take only the pre-created generation's shared advisory lock and will not
acquire a writer-operation lease.

Activation, migration, promotion, rollback, repair, pointer recovery, and GC
share one fenced workspace-operation domain. `SEMANTIC_CLAIM` has its reserved
semantic domain. Each live domain retains its own accepted operation epoch, so
allocating a semantic claim cannot invalidate or strand an otherwise-current
workspace lease. P2 allocates and validates these leases but performs none of
the deferred P3-P5 operations named by them.
