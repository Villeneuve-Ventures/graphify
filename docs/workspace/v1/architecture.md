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

P1 has no registry writer, lease allocator, generation builder, pointer mover,
journal appender, GC implementation, freshness scanner, semantic queue, service,
or workspace CLI. Those are dependency-ordered P2-P5 work.

## Authority split

Repo-owned `.graphify/workspace.toml` contains the required
`contract = "graphify.workspace.config"` discriminator, `schema_version = 1`,
the immutable `repo_uuid` label, and policy only. It cannot select lifecycle
paths, pointers, generations, or global state. Enrollment remains
operator-authorized in P2; possession of a copied UUID is not proof of identity.

The future global registry has one singular `active_source` per workspace plus
a monotonic `active_source_revision`, UUID-enrollment evidence, and
revision/source-bound active-source evidence. The active-source evidence stamps
the distinct operation epoch and accepted workspace-operation fence token that
a future activation CAS must revalidate. Paths, worktree coordinates, and
normalized remote URLs remain discovery aliases, not stable identity. A query
never guesses among aliases.

Generated lifecycle state is external by default:

```text
~/.local/state/graphify/
  registry.json
  registry.previous.json
  workspaces/<repo_uuid>/
    workspace.json
    generations/<generation-id>/
      graphify-out/
      receipt.json
    journal/
    pointers.json
    pointers.previous.json
    locks/generations/<generation-id>.lock
    quarantine/
    migrations/
```

## Ordering frozen for later implementation

When required, lock ordering is global registry, exclusive fenced workspace
operation, generation coordination locks in lexical generation-ID order, then
pointer validation/CAS. Queries take only the pre-created generation's shared
advisory lock and do not acquire a writer-operation lease.

Activation, migration, promotion, rollback, repair, pointer recovery, and GC
share one fenced workspace-operation domain. P1 records this contract; it does
not implement the operations.
