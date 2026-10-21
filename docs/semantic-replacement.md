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


## Deletion-only runbook publication

A deletion-only batch skips extraction and overwrites the current aggregate with
empty records and `replacement_sources: []`. Prior aggregate/chunk files cannot
establish a current replacement. The shared and inline update sections use the
same exact prepared-token runner and private output namespace as the build.
Detection, merge ownership and manifest stamping use that owner's canonical
scan root. Relative inputs such as `.` and `repo`, including directory aliases,
are resolved by the handoff before entering the prepared output.
Before each detection attempt, the runbook restores every published artifact
through the existing update baseline and removes private files absent from that
inventory. This also resets prior cost/report/export mutations and extraction
scratch files. Fresh detection and extraction then populate the new attempt;
optional outputs omitted on retry retain their published bytes.
Each merge attempt restores the published graph and manifest through the exact live
update owner and existing preserved receipt. A failed attempt's staged graph
cannot become retained input on retry. The graph-diff baseline uses that same
published predecessor, including retries after the Step 9 body has removed its
previous private copy. Step 9 removes that baseline before finalization, so it
cannot publish deleted records as a backup.

Only a deletion-only batch (`new_total == 0`) may explicitly mark a validated
prune-to-empty result with `complete_empty_prune`. This is distinct from successful semantic extraction;
the later empty-graph guard requires `accepted_merge` for that prune marker.
Undeclared empty input still refuses. The ownership and prune precedence above
are unchanged.

The semantic update entry starts its own handoff against the certified published
snapshot. Before extraction work, it prints a shell-quoted export assignment for
that exact token. Keep the printed assignment; prepend it to the detection block
in the same fresh-shell invocation when retrying. A retry requires the matching
operation, root and output; it cannot take over another pending operation. A no-op cancels its
unpublished handoff when it has a certified predecessor. A valid, uncoordinated
receiptless legacy no-op is detected before opening a transaction and leaves the published graph
and manifest unchanged; detection may warm its normal stat cache. A changed
legacy corpus still starts its first update generation.
That first update binds the legacy inventory's names, presence and content digests
in its existing live-owner record and immutable token. Before preparing or
retrying, it refuses changed graph, manifest or owned export bytes, including
newly discovered owned exports. Unchanged retries still use the published
inventory; failed prepared outputs cannot become the baseline. Older pending
receiptless updates without this binding are refused rather than adopting their
current public files. Full builds and receipt-backed updates keep their existing
record shapes and admission rules; no separate recovery record is introduced.
The binding is also persisted in the existing bootstrap protocol until its live
owner is installed. Recovery and takeover revalidate the admitted legacy inventory
before changing ownership or discarding prepared work. A recovered bound legacy
update keeps publication generation 1 and its original digest while rotating the
owner and claim epoch; an execution retry does not establish a new published
baseline. Pending-transition replay must carry that same binding. Older pending
records with an already-bound successor remain replayable; an unbound legacy
bootstrap cannot infer authority from the current public files. Full, graphless
and receipt-backed recovery retain their existing generation rules and formats.
If a receipt appears before the legacy owner's protocol is complete, recovery
refuses to reinterpret it as a new baseline. The original exact owner can still
finish its interrupted prepared publication through the existing finalizer.
The semantic entry may admit a completed receipt whose drainer file is missing,
using the existing missing-drainer validation. Begin revalidates the same snapshot
and preserves a receipt-bound complete predecessor before taking ownership. A
no-op restores that validated completed drainer; graph and manifest bytes remain
unchanged. Other readers and partial or live ownership states keep their existing
refusals. This does not introduce a new recovery record or protocol.

Prepared semantic updates retain their existing 512 MiB graph-read bound, even
when `GRAPHIFY_MAX_GRAPH_BYTES` is raised for ordinary readers. The entry refuses
an oversized admitted graph before creating a handoff, leaving publication and
ownership bytes unchanged. The configured lower graph cap still applies first.

Detection reads the published manifest even after Step 9
has staged a new one, so a retry still sees every unpublished deletion.

Merge preparation stages completion-filtered `manifest_files` with the accepted
graph; it does not save the manifest. Step 9 saves that manifest after successful
graph/report generation, in the existing prepared publication. A failed Step 9
body stops before finalization. Refused or interrupted work leaves the published
graph and manifest unchanged, so the next update still sees pending deletions;
a successful retry finalizes the existing prepared generation. No new recovery
protocol is introduced.

A validated deletion-only prune-to-empty also invalidates prior managed HTML,
SVG, GraphML, Cypher, callflow/tree views and wiki/Obsidian files in the private
output before optional exports run. Owned Obsidian manifests in the prior and
prepared inventories select custom managed vault notes and canvases, including
nested vaults and vaults at the output root; unrelated managed artifacts remain.
Owned callflow and tree documents are selected by their generated HTML signatures, so
project-derived default names and custom destinations beneath the managed output
are covered. Unowned files with the same content are not selected.
The existing publication plan applies those
removals only when finalization succeeds; a failed attempt preserves the public
exports. Regenerated outputs can join the new generation. Unmanaged public files
and external export destinations remain outside this invalidation.

When `--wiki` is requested, prepared runbooks skip that optional export for an
accepted, node-empty `complete_empty_prune` result owned by an update transaction.
The invalidated managed wiki stays absent, and Step 9 can finalize the deletion.
Nonempty wiki exports still run. Ordinary empty-community or stale-community
refusals in the CLI and `to_wiki` are unchanged; a flag alone, a successful empty
replacement or a full-build owner does not establish this deletion-only skip.
