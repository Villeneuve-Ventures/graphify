# Semantic source replacement

`build_merge` composes a graph in memory. It does not write its source graph;
publication remains the caller's responsibility. Conflicting retained node,
edge-slot or hyperedge identities refuse composition before export.

## Caller declaration

`replacement_sources` is a keyword-only list of source paths. It is the entire
set of sources whose extraction completed successfully and whose old contribution
may be replaced. Pass `[]` to add compatible claims without replacing any source.
Pass a source even when its complete extraction has no nodes, edges or hyperedges:
this removes that source's old contribution. Missing or failed extraction must
not be represented as a successful empty result. The caller owns that distinction.
A relationship's `source_file` never establishes replacement authority.

Omitting the argument (`None`) preserves the legacy behavior: fresh node
`source_file` values establish replacement, including before identity admission.
New callers should use the explicit list. It must not be combined with
`ast_refresh_sources`; AST refresh keeps its separate existing contract.

Ownership comparisons normalize slashes, dot segments, and in-root absolute paths
against the explicit or inferred scan root, including aliases of that root. File
symlink names remain distinct owners even when their bytes share a target.
Retained payload spelling is preserved.
Node ownership also recognizes the legacy `source` field. Malformed declarations
and non-string record ownership refuse composition; absent ownership grants no
replacement authority. Normalization is an ownership key, not permission to read
or delete a filesystem path.

## Precedence and preservation

1. Replacement wins over an explicit prune for the same normalized source. This
   preserves the tested legacy node-based replacement/prune precedence.
2. For every other pruned source, discard retained and fresh nodes, edges and
   hyperedges. This corrects the old case where a fresh hyperedge survived a prune.
3. Remove replaced sources' old nodes and owned relationships. Keep foreign-owned
   nodes and relationships; retired node endpoints are removed from retained
   relationships under the existing endpoint-survival rules.
4. Normalize and deduplicate fresh extraction only. Compatible additional claims
   may refer to retained entities, including their supported aliases. A declared
   replacement cannot redefine a foreign-owned identity or overwrite its edge
   slot or named group. Directed and undirected slot semantics are unchanged.

## Incremental extraction and caches

Semantic extraction reports `completed_sources` explicitly. A file may acknowledge
successful empty extraction. Omission, invalid JSON, failed reads, capped text,
missing image input, output truncation and incomplete slices grant no completion.
All slices of a source must complete. Runtime completion is bounded by consumed
and dispatched inputs; the progress callback alone is insufficient.

The incremental CLI passes those completions and its existing AST/structured
source replacements to `build_merge`. Its code-only AST-refresh path is unchanged.
Optional learning remains optional. Successful empty semantic files are stamped
in the manifest and cached, so they are not repeatedly dispatched.

Cache entries gain `complete_source: true` and a normalized
`complete_source_owner` only when saved as a final complete
extraction with `completed_source_files`. Unioned checkpoints cannot establish
completeness. Replacement callers request `require_complete=True`; legacy and
partial entries are re-extracted. The owner binding prevents a physical cache
hash shared through file symlinks from granting another logical source replacement. Default cache APIs still read legacy claims.
Source ownership and hash keys stay anchored to the scan root; an optional
`cache_root` selects storage separately. A custom CLI `--out` must not turn
relative-owner records into a certified empty cache entry. Cache filtering that
drops relationships also revokes completeness. Standard and deep namespaces remain
separate. These fields describe extraction
completion, not authorization for filesystem operations.

For explicitly completed fragments, semantic cleanup preserves valid named
concepts and defers external hyperedge members to build admission against retained
identities. Undeclared fragments keep the legacy cleanup behavior. This prevents
cleanup from turning a valid non-empty refresh into an empty replacement.

Generated update runbooks carry explicit completion through chunk collection,
cache merge and Part C into `replacement_sources`. Only current successful
extraction results may populate the completion list; stale chunk files are not
completion evidence. Extraction specifications request the same declaration.

Chunk handoffs keep relative completion declarations unbound until their consumer
supplies the scan root; `merge-chunks` does not substitute its working directory.
An empty response with no acknowledgement of consumed input still takes hollow
response recovery. Relative cache roots use the same ownership key as absolute
roots, without resolving distinct file-symlink owners into one source.

Generated B0 resets the current batch's success manifest and direct aggregate in
the exact prepared output namespace. File collectors register absolute chunk paths
there through the prepared-token runner. Direct collectors stage their aggregate
in the same namespace; B3 keeps it and concatenates chunk files only as a fallback.

Extraction entry points anchor filesystem input paths once before binding consumed
files, dispatched slices and cache scope to the scan root. Logical file-symlink
owners remain distinct. Cache writes containing unowned records retain useful
scoped claims but cannot certify a complete replay, including over an older
complete entry. A genuine successful-empty extraction still qualifies.

Devin keeps semantic completion through its final sanitize before emitting the
replacement declaration. Later report, export and graph-diff stages restore an
accepted merge without interpreting retained identities again; fresh extraction
continues through the normal build path.
